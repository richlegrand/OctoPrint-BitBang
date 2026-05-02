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
        wrapper.style.cssText = "position:relative;display:inline-block;width:100%;pointer-events:auto";

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
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ value: parseInt(slider.value, 10) })
                }).catch(function (err) {
                    console.log("[BitBang] Brightness update failed:", err);
                });
            }, 150);
        };

        container.appendChild(icon);
        container.appendChild(slider);
        wrapper.appendChild(container);
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

    function replaceWebcam(video) {
        video.style.width = "100%";
        video.style.backgroundColor = "#000";

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
                    "{ display: none !important; }";
                document.head.appendChild(style);
            }
            classicContainer.appendChild(video);
            addFullscreenButton(video);
            applyCameraConfig(video);
            return true;
        }

        // Fallback for other layouts: replace #webcam_image in place.
        var img = document.getElementById("webcam_image");
        if (!img) return false;
        img.parentNode.replaceChild(video, img);
        addFullscreenButton(video);
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

    if (isBitBang) {
        // Remote mode: bootstrap.js wires the track via data-bitbang-stream
        function injectRemote() {
            if (document.querySelector("video[data-bitbang-stream]")) return;
            var video = document.createElement("video");
            video.setAttribute("data-bitbang-stream", "camera");
            video.autoplay = true;
            video.playsinline = true;
            video.muted = true;
            replaceWebcam(video);
        }

        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", injectRemote);
        } else {
            injectRemote();
        }

        var observer = new MutationObserver(function () {
            if (document.getElementById("webcam_image")) {
                injectRemote();
            }
        });
        observer.observe(document.body, { childList: true, subtree: true });

    } else {
        // Local mode: direct WebRTC to the plugin's signaling endpoint
        function startLocalVideo() {
            if (document.querySelector("video[data-bitbang-local]")) return;
            var video = document.createElement("video");
            video.setAttribute("data-bitbang-local", "1");
            video.autoplay = true;
            video.playsinline = true;
            video.muted = true;
            if (!replaceWebcam(video)) return;

            var pc = new RTCPeerConnection();

            pc.ontrack = function (event) {
                if (event.streams && event.streams[0]) {
                    video.srcObject = event.streams[0];
                } else {
                    if (!video.srcObject) video.srcObject = new MediaStream();
                    video.srcObject.addTrack(event.track);
                }
            };

            // Need to add a transceiver to receive video
            pc.addTransceiver("video", { direction: "recvonly" });

            pc.createOffer().then(function (offer) {
                return pc.setLocalDescription(offer);
            }).then(function () {
                return fetch("/plugin/bitbang/offer", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        sdp: pc.localDescription.sdp,
                        type: pc.localDescription.type
                    })
                });
            }).then(function (response) {
                return response.json();
            }).then(function (answer) {
                if (answer.error) {
                    console.log("[BitBang] Local video not available:", answer.error);
                    return;
                }
                return pc.setRemoteDescription(answer);
            }).catch(function (err) {
                console.log("[BitBang] Local video failed:", err);
            });
        }

        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", startLocalVideo);
        } else {
            startLocalVideo();
        }

        var observer = new MutationObserver(function () {
            if (document.getElementById("webcam_image")) {
                startLocalVideo();
            }
        });
        observer.observe(document.body, { childList: true, subtree: true });
    }
})();
