"""MediaStreamTrack wrapper that hflip/vflips frames via PyAV.

Used for USB webcams where flip can't be done at the sensor level.
aiortc's MediaPlayer already decodes frames to av.VideoFrame before
handing them to the H.264 encoder, so we intercept there and flip
in-place.
"""

from aiortc import MediaStreamTrack


class FlippedTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, source, hflip=False, vflip=False):
        super().__init__()
        self._source = source
        self._hflip = bool(hflip)
        self._vflip = bool(vflip)
        self._graph = None
        self._buffer_src = None
        self._buffer_sink = None

    def _init_graph(self, frame):
        # Build a PyAV filter graph matching the first frame's format.
        # Chaining hflip and vflip gives us a 180° rotation when both are set.
        from av.filter import Graph

        graph = Graph()
        src = graph.add_buffer(template=frame)
        last = src
        if self._hflip:
            n = graph.add("hflip")
            last.link_to(n)
            last = n
        if self._vflip:
            n = graph.add("vflip")
            last.link_to(n)
            last = n
        sink = graph.add("buffersink")
        last.link_to(sink)
        graph.configure()
        self._graph = graph
        self._buffer_src = src
        self._buffer_sink = sink

    async def recv(self):
        frame = await self._source.recv()
        if not (self._hflip or self._vflip):
            return frame
        if self._graph is None:
            self._init_graph(frame)
        self._buffer_src.push(frame)
        return self._buffer_sink.pull()

    def stop(self):
        super().stop()
        if self._source and hasattr(self._source, "stop"):
            self._source.stop()
