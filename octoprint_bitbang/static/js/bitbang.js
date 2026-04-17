/*
 * OctoPrint-BitBang - video injection
 *
 * When OctoPrint is accessed remotely via BitBang, replaces the MJPEG
 * webcam <img> with an H.264 <video> element wired to the WebRTC stream.
 * Local access is unaffected.
 */
(function () {
    // Only activate when loaded via BitBang (service worker present)
    if (!navigator.serviceWorker || !navigator.serviceWorker.controller) {
        return;
    }

    function replaceWebcam() {
        var img = document.getElementById("webcam_image");
        if (!img) {
            return;
        }

        var video = document.createElement("video");
        video.setAttribute("data-bitbang-stream", "camera");
        video.autoplay = true;
        video.playsinline = true;
        video.muted = true;
        video.style.width = "100%";
        img.parentNode.replaceChild(video, img);
    }

    // OctoPrint loads UI dynamically, so wait for the webcam element
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", replaceWebcam);
    } else {
        replaceWebcam();
    }

    // Also watch for late-loading webcam tab
    var observer = new MutationObserver(function () {
        if (document.getElementById("webcam_image")) {
            replaceWebcam();
        }
    });
    observer.observe(document.body, { childList: true, subtree: true });
})();
