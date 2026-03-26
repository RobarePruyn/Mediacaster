"""
Encoding profiles — default bitrate table, validation, and transcode ladder logic.

Provides centralized encoding profile management for per-stream settings:
  - Default bitrate lookup based on resolution/framerate/codec combination
  - Validation rules (4K requires H.265, 4K requires playlist source, etc.)
  - Transcode ladder computation (which renditions to generate for an upload)
  - CPU cost estimation for capacity enforcement

The bitrate defaults are tuned for clean MPEG-TS multicast output — high
enough for sharp text and motion, low enough to avoid encoder throughput
issues on typical server hardware.
"""

import logging

logger = logging.getLogger("encoding_profiles")

# ── Resolution constants ─────────────────────────────────────────────────────

RESOLUTION_4K = "3840x2160"
RESOLUTION_1080P = "1920x1080"
RESOLUTION_720P = "1280x720"

VALID_RESOLUTIONS = {RESOLUTION_4K, RESOLUTION_1080P, RESOLUTION_720P}
VALID_CODECS = {"h264", "h265"}
VALID_FRAMERATES = {30, 60}

# Resolution hierarchy for the transcode ladder (highest to lowest)
RESOLUTION_ORDER = [RESOLUTION_4K, RESOLUTION_1080P, RESOLUTION_720P]

# Map resolution strings to pixel widths for comparison
RESOLUTION_WIDTHS = {
    RESOLUTION_4K: 3840,
    RESOLUTION_1080P: 1920,
    RESOLUTION_720P: 1280,
}


# ── Default bitrate table ────────────────────────────────────────────────────
# Keyed by (resolution, framerate, codec). Values are ffmpeg bitrate strings.
# These produce clean output at each tier without overloading the encoder.

DEFAULT_BITRATES = {
    (RESOLUTION_720P, 30, "h264"): "4M",
    (RESOLUTION_720P, 60, "h264"): "6M",
    (RESOLUTION_720P, 30, "h265"): "3M",
    (RESOLUTION_720P, 60, "h265"): "4M",
    (RESOLUTION_1080P, 30, "h264"): "8M",
    (RESOLUTION_1080P, 60, "h264"): "12M",
    (RESOLUTION_1080P, 30, "h265"): "5M",
    (RESOLUTION_1080P, 60, "h265"): "8M",
    (RESOLUTION_4K, 30, "h265"): "15M",
    (RESOLUTION_4K, 60, "h265"): "20M",
    # 4K H.264 not recommended but included as a fallback
    (RESOLUTION_4K, 30, "h264"): "30M",
    (RESOLUTION_4K, 60, "h264"): "40M",
}


# ── CPU cost estimates (percent of one logical core) ─────────────────────────
# Empirical estimates for real-time x11grab encoding. Used by the capacity
# enforcement system to predict whether a new stream will exceed the server's
# headroom. Playlist streams are near-zero (stream copy, no encoding).

CPU_COST_ESTIMATES = {
    # (resolution, framerate, codec): estimated_cpu_percent
    ("playlist", None, None): 0.5,
    (RESOLUTION_720P, 30, "h264"): 15.0,
    (RESOLUTION_720P, 60, "h264"): 25.0,
    (RESOLUTION_720P, 30, "h265"): 25.0,
    (RESOLUTION_720P, 60, "h265"): 40.0,
    (RESOLUTION_1080P, 30, "h264"): 30.0,
    (RESOLUTION_1080P, 60, "h264"): 50.0,
    (RESOLUTION_1080P, 30, "h265"): 45.0,
    (RESOLUTION_1080P, 60, "h265"): 70.0,
}


def get_effective_bitrate(resolution: str, framerate: int, codec: str,
                          override: str | None = None) -> str:
    """Return the bitrate override if set, otherwise look up the default.

    Args:
        resolution: Stream resolution (e.g. "1920x1080")
        framerate: 30 or 60
        codec: "h264" or "h265"
        override: Admin-provided bitrate override (e.g. "10M"), or None

    Returns:
        Bitrate string for ffmpeg (e.g. "8M")
    """
    if override:
        return override
    key = (resolution, framerate, codec)
    return DEFAULT_BITRATES.get(key, "8M")


def get_effective_gop_size(framerate: int, override: int | None = None) -> int:
    """Return the GOP override if set, otherwise default to 1 second of frames.

    Args:
        framerate: 30 or 60
        override: Admin-provided GOP size, or None

    Returns:
        GOP size in frames
    """
    if override is not None:
        return override
    return framerate


def validate_stream_profile(source_type: str, resolution: str, codec: str,
                            framerate: int) -> str | None:
    """Validate encoding profile constraints. Returns error message or None.

    Rules:
      - Resolution must be one of the valid options
      - Codec must be h264 or h265
      - Framerate must be 30 or 60
      - 4K resolution requires H.265 codec
      - 4K resolution requires playlist source type (too CPU-intensive for live)
      - Container-based streams (browser/presentation) max at 1080p
    """
    if resolution not in VALID_RESOLUTIONS:
        return f"Invalid resolution '{resolution}'. Must be one of: {', '.join(sorted(VALID_RESOLUTIONS))}"
    if codec not in VALID_CODECS:
        return f"Invalid codec '{codec}'. Must be 'h264' or 'h265'"
    if framerate not in VALID_FRAMERATES:
        return f"Invalid framerate {framerate}. Must be 30 or 60"

    if resolution == RESOLUTION_4K:
        if codec != "h265":
            return "4K resolution requires H.265 codec"
        if source_type != "playlist":
            return "4K resolution is only supported for playlist streams (too CPU-intensive for live capture)"

    if source_type in ("browser", "presentation"):
        if resolution == RESOLUTION_4K:
            return "Container-based streams (browser/presentation) cannot exceed 1080p"

    return None


def get_transcode_ladder(native_width: int, native_height: int,
                         ladder_config: dict) -> list[tuple[str, str]]:
    """Determine which renditions to generate based on native resolution and config.

    Args:
        native_width: Source video width in pixels
        native_height: Source video height in pixels
        ladder_config: Dict of tier_name: enabled pairs, e.g.
                       {"720p_h264": True, "1080p_h264": True, "1080p_h265": False, "4k_h265": False}

    Returns:
        List of (resolution, codec) tuples to transcode, ordered highest to lowest
    """
    # Classify native resolution into a tier
    if native_width >= 3840 or native_height >= 2160:
        native_tier = RESOLUTION_4K
    elif native_width >= 1920 or native_height >= 1080:
        native_tier = RESOLUTION_1080P
    else:
        native_tier = RESOLUTION_720P

    native_idx = RESOLUTION_ORDER.index(native_tier)

    # All possible rendition tiers (resolution, codec, config_key)
    all_tiers = [
        (RESOLUTION_4K, "h265", "4k_h265"),
        (RESOLUTION_1080P, "h264", "1080p_h264"),
        (RESOLUTION_1080P, "h265", "1080p_h265"),
        (RESOLUTION_720P, "h264", "720p_h264"),
    ]

    renditions = []
    for res, codec, config_key in all_tiers:
        # Skip renditions above the native resolution (don't upscale)
        res_idx = RESOLUTION_ORDER.index(res)
        if res_idx < native_idx:
            continue
        # Skip renditions disabled in the ladder config
        if not ladder_config.get(config_key, False):
            continue
        renditions.append((res, codec))

    # Always include at least one rendition at native resolution with H.264
    # even if nothing is enabled in the config (safety fallback)
    if not renditions:
        renditions.append((native_tier, "h264"))

    return renditions


def estimate_cpu_cost(source_type: str, resolution: str, framerate: int,
                      codec: str) -> float:
    """Estimate CPU usage as a percentage for a stream with the given profile.

    Playlist streams are near-zero (stream copy). Container-based streams
    depend on resolution, framerate, and codec. Returns a percentage of
    total system CPU.
    """
    if source_type == "playlist":
        return CPU_COST_ESTIMATES.get(("playlist", None, None), 0.5)

    key = (resolution, framerate, codec)
    return CPU_COST_ESTIMATES.get(key, 30.0)
