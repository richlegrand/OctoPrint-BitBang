/*
 * OctoPrint-BitBang - H.264 video for OctoPrint
 *
 * Two modes:
 * - Remote (via BitBang): replaces MJPEG <img> with <video> wired to
 *   BitBang's WebRTC stream (bootstrap.js handles the track)
 * - Local (direct access): creates a WebRTC peer connection to the
 *   plugin's /offer endpoint for H.264 video on the LAN
 */
(function () {
    var isBitBang = !!window.__bbSessionId;

    function addFullscreenButton(video) {
        var wrapper = document.createElement("div");
        wrapper.style.cssText = "position:relative;display:block;width:100%;pointer-events:auto";

        var btn = document.createElement("button");
        btn.className = "btn btn-mini";
        btn.style.cssText = "position:absolute;top:8px;right:8px;z-index:10;opacity:0.6;cursor:pointer;pointer-events:auto";
        btn.innerHTML = '<i class="fas fa-expand"></i>';
        btn.title = "Fullscreen";
        btn.onmouseover = function () { btn.style.opacity = "1"; };
        btn.onmouseout = function () { btn.style.opacity = "0.6"; };
        btn.onclick = function () {
            if (document.fullscreenElement) {
                document.exitFullscreen();
                return;
            }
            var fn = video.requestFullscreen || video.webkitRequestFullscreen ||
                     video.webkitEnterFullscreen || video.msRequestFullscreen;
            if (fn) {
                var ret = fn.call(video);
                if (ret && ret.catch) {
                    ret.catch(function (err) {
                        console.log("[BitBang] Fullscreen failed:", err);
                    });
                }
            }
        };

        video.parentNode.insertBefore(wrapper, video);
        wrapper.appendChild(video);
        wrapper.appendChild(btn);

        // Hide the fullscreen button until live video appears (keep the
        // "Connecting…" overlay clean).
        btn.style.display = "none";
        var reveal = function () { btn.style.display = ""; };
        if (!video.paused && video.readyState >= 2) reveal();
        else video.addEventListener("playing", reveal);
    }

    // Show a status overlay while the WebRTC video establishes (connection +
    // camera take a few seconds), driven by the <video> element's own media
    // events so it always reflects reality.
    function addStatusOverlay(video) {
        var wrap = video.parentNode;
        if (!wrap) return;

        if (!document.getElementById("bb-spin-style")) {
            var st = document.createElement("style");
            st.id = "bb-spin-style";
            st.textContent = "@keyframes bb-spin{to{transform:rotate(360deg)}}";
            document.head.appendChild(st);
        }

        var overlay = document.createElement("div");
        overlay.style.cssText =
            "position:absolute;inset:0;display:flex;flex-direction:column;" +
            "align-items:center;justify-content:center;gap:12px;color:#bbb;" +
            "background:#000;font-size:14px;pointer-events:none;z-index:5;";
        var spinner = document.createElement("div");
        spinner.style.cssText =
            "width:30px;height:30px;border:3px solid #444;border-top-color:#bbb;" +
            "border-radius:50%;animation:bb-spin 0.8s linear infinite;";
        var label = document.createElement("div");
        label.textContent = "Connecting to camera…";
        overlay.appendChild(spinner);
        overlay.appendChild(label);
        wrap.appendChild(overlay);

        var live = false;
        function show(msg, spin) {
            overlay.style.display = "flex";
            label.textContent = msg;
            spinner.style.display = spin ? "block" : "none";
        }
        function hide() { live = true; overlay.style.display = "none"; }

        video.addEventListener("playing", hide);
        video.addEventListener("loadeddata", hide);
        video.addEventListener("waiting", function () { if (live) show("Buffering…", true); });
        video.addEventListener("stalled", function () { if (live) show("Reconnecting…", true); });

        // If video never starts, stop spinning and tell the user.
        setTimeout(function () { if (!live) show("Camera unavailable", false); }, 25000);
    }

    function addBrightnessControl(wrapper, initialValue) {
        var container = document.createElement("div");
        container.style.cssText = "position:absolute;bottom:8px;right:8px;display:flex;align-items:center;gap:6px;opacity:0.6;z-index:10;pointer-events:auto;transition:opacity 0.2s;";
        container.onmouseover = function () { container.style.opacity = "1"; };
        container.onmouseout = function () { container.style.opacity = "0.6"; };

        var icon = document.createElement("i");
        icon.className = "fas fa-sun";
        icon.style.color = "#fff";
        icon.title = "Brightness";

        var slider = document.createElement("input");
        slider.type = "range";
        slider.min = "-100";
        slider.max = "100";
        slider.step = "5";
        slider.value = String(initialValue || 0);
        slider.style.cssText = "flex:1;max-width:200px;cursor:pointer;";

        var debounce;
        slider.oninput = function () {
            clearTimeout(debounce);
            debounce = setTimeout(function () {
                fetch("/plugin/bitbang/camera/brightness", {
                    method: "POST",
                    headers: OctoPrint.getRequestHeaders("POST", { "Content-Type": "application/json" }),
                    body: JSON.stringify({ value: parseInt(slider.value, 10) })
                }).catch(function (err) {
                    console.log("[BitBang] Brightness update failed:", err);
                });
            }, 150);
        };

        container.appendChild(icon);
        container.appendChild(slider);
        wrapper.appendChild(container);

        // Hide the brightness control until live video appears (don't show it
        // floating over the "Connecting…" overlay).
        var v = wrapper.querySelector("video");
        if (v) {
            container.style.display = "none";
            var reveal = function () { container.style.display = "flex"; };
            if (!v.paused && v.readyState >= 2) reveal();
            else v.addEventListener("playing", reveal);
        }
    }

    function applyCameraConfig(video) {
        // Flip is applied at the picamera2 sensor level (baked into the
        // bitstream) so no CSS transform is needed here. We only fetch
        // brightness to seed the slider's initial position.
        fetch("/plugin/bitbang/camera/config").then(function (r) {
            return r.json();
        }).then(function (cfg) {
            if (video.parentNode) {
                addBrightnessControl(video.parentNode, cfg.brightness);
            }
        }).catch(function () {});
    }

    // Keep the video filling its box at the real stream aspect. A WebRTC
    // <video> has no dimensions until the first frame, so if it binds early the
    // browser can cache a placeholder/half-size layout and not reflow (→ a small
    // video with black around it, needing a fresh tab). Re-assert sizing and
    // force a reflow whenever real dimensions arrive so it self-corrects.
    function applyVideoSizing(video) {
        function set() {
            video.style.width = "100%";
            video.style.height = "auto";
            video.style.maxWidth = "100%";
            video.style.objectFit = "contain";
            video.style.display = "block";
        }
        set();
        function reflow() {
            set();
            // Collapse then restore height within one frame: forces the browser
            // to recompute from the new intrinsic aspect, no visible flicker.
            video.style.height = "0px";
            void video.offsetHeight;
            video.style.height = "auto";
        }
        video.addEventListener("loadedmetadata", reflow);
        video.addEventListener("resize", reflow);
    }

    function replaceWebcam(video) {
        video.style.backgroundColor = "#000";
        applyVideoSizing(video);

        // OctoPrint 1.11+ Classic Webcam hides its default containers until
        // a stream URL is configured. Mount into the outer container so
        // we're visible regardless of the user's webcam settings.
        var classicContainer = document.getElementById("classicwebcam_container");
        if (classicContainer) {
            // Knockout visibility bindings on classicwebcam's built-in
            // containers keep re-showing them, so use a stylesheet rule
            // which beats Knockout's inline style.display assignments.
            if (!document.getElementById("bitbang-hide-classicwebcam")) {
                var style = document.createElement("style");
                style.id = "bitbang-hide-classicwebcam";
                style.textContent =
                    "#webcam_video_container, #webcam_img_container " +
                    "{ display: none !important; }" +
                    "#classicwebcam_container " +
                    "{ padding: 0 !important; line-height: 0 !important; font-size: 0 !important; }";
                document.head.appendChild(style);
            }
            classicContainer.appendChild(video);
            addFullscreenButton(video);
            addStatusOverlay(video);
            applyCameraConfig(video);
            return true;
        }

        // Fallback for other layouts: replace #webcam_image in place.
        var img = document.getElementById("webcam_image");
        if (!img) return false;
        img.parentNode.replaceChild(video, img);
        addFullscreenButton(video);
        addStatusOverlay(video);
        applyCameraConfig(video);
        return true;
    }

    // Intercept download links that use absolute URLs. OctoPrint
    // generates these with the BitBang host, but clicking them navigates
    // outside the iframe/SW scope. Use fetch + blob instead.
    if (isBitBang) {
        document.addEventListener("click", function (e) {
            var link = e.target.closest("a[href]");
            if (!link) return;
            var href = link.getAttribute("href");
            if (!href || !href.match(/\/downloads\//)) return;

            e.preventDefault();
            var filename = href.split("/").pop();
            fetch(href).then(function (r) {
                if (!r.ok) throw new Error("Download failed");
                return r.blob();
            }).then(function (blob) {
                var url = URL.createObjectURL(blob);
                var a = document.createElement("a");
                a.href = url;
                a.download = decodeURIComponent(filename);
                a.click();
                URL.revokeObjectURL(url);
            }).catch(function (err) {
                console.log("[BitBang] Download failed:", err);
            });
        }, true);
    }

    // Connect video via local offer/answer endpoint. Works on LAN and
    // remotely (fetches TURN servers). Can be called with an existing
    // video element (fallback from remote mode) or creates its own.
    function connectLocalVideo(video, attempt) {
        // On a fresh page load the OctoPrint session's CSRF token may not be
        // established yet, so the first POST /plugin/bitbang/offer can come back
        // 400/403. Retry a bounded number of times so the stream comes up on its
        // own instead of requiring a manual page refresh.
        attempt = attempt || 0;
        var MAX_ATTEMPTS = 10;
        var RETRY_MS = 1500;
        var pc = null;
        function retry(why) {
            if (pc) { try { pc.close(); } catch (e) {} }
            if (attempt + 1 < MAX_ATTEMPTS) {
                setTimeout(function () { connectLocalVideo(video, attempt + 1); }, RETRY_MS);
            } else {
                console.log("[BitBang] Local video gave up after retries:", why);
            }
        }
        fetch("/plugin/bitbang/ice-servers").then(function (r) {
            return r.json();
        }).then(function (iceServers) {
            var config = (iceServers && iceServers.length > 0) ? { iceServers: iceServers } : {};
            pc = new RTCPeerConnection(config);

            pc.ontrack = function (event) {
                if (event.streams && event.streams[0]) {
                    video.srcObject = event.streams[0];
                } else {
                    if (!video.srcObject) video.srcObject = new MediaStream();
                    video.srcObject.addTrack(event.track);
                }
            };

            pc.addTransceiver("video", { direction: "recvonly" });

            return pc.createOffer().then(function (offer) {
                return pc.setLocalDescription(offer);
            }).then(function () {
                return fetch("/plugin/bitbang/offer", {
                    method: "POST",
                    headers: OctoPrint.getRequestHeaders("POST", { "Content-Type": "application/json" }),
                    body: JSON.stringify({
                        sdp: pc.localDescription.sdp,
                        type: pc.localDescription.type
                    })
                });
            }).then(function (response) {
                if (!response.ok) {
                    // 400 (CSRF token not ready yet), 403 (not logged in yet), etc.
                    retry("offer HTTP " + response.status);
                    return;
                }
                return response.json().then(function (answer) {
                    if (answer.error) {
                        console.log("[BitBang] Local video not available:", answer.error);
                        return;
                    }
                    return pc.setRemoteDescription(answer);
                });
            });
        }).catch(function (err) {
            retry(String(err));
        });
    }

    function createVideo(attrs) {
        var video = document.createElement("video");
        video.autoplay = true;
        video.playsInline = true;
        video.muted = true;
        for (var k in attrs) video.setAttribute(k, attrs[k]);
        return video;
    }

    function whenWebcamReady(callback) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", callback);
        } else {
            callback();
        }
        var observer = new MutationObserver(function () {
            if (document.getElementById("webcam_image") ||
                document.getElementById("classicwebcam_container")) {
                callback();
            }
        });
        observer.observe(document.body, { childList: true, subtree: true });
    }

    if (isBitBang) {
        // Remote mode: bootstrap.js wires the track via data-bitbang-stream
        whenWebcamReady(function () {
            if (document.querySelector("video[data-bitbang-stream]")) return;
            var video = createVideo({"data-bitbang-stream": "camera"});
            replaceWebcam(video);
        });
    } else {
        // Local mode: direct WebRTC to the plugin's signaling endpoint
        whenWebcamReady(function () {
            if (document.querySelector("video[data-bitbang-local]")) return;
            var video = createVideo({"data-bitbang-local": "1"});
            if (!replaceWebcam(video)) return;
            connectLocalVideo(video);
        });
    }
})();
