"""Decode GTFS-RT protobuf bytes to a plain Python dict."""
try:
    from gtfs_realtime_bindings.protobuf import gtfs_realtime_pb2
    from google.protobuf.json_format import MessageToDict
    PROTO_AVAILABLE = True
except ImportError:
    PROTO_AVAILABLE = False


def decode_gtfs_rt(data: bytes) -> dict | None:
    """
    Decode a GTFS-RT FeedMessage protobuf.
    Returns dict on success, None if decoding fails or library missing.
    """
    if not PROTO_AVAILABLE:
        return None
    try:
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(data)
        return MessageToDict(feed, preserving_proto_field_name=True, including_default_value_fields=False)
    except Exception:
        return None
