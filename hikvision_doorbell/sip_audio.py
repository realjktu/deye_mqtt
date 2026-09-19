"""Audio helpers for answering a SIP call: G.711 encoding, SDP negotiation and RTP.

Kept free of external dependencies. G.711 mu-law encoding is implemented here rather than
via the standard library's audioop, because audioop is deprecated and was removed in
Python 3.13.
"""

import logging
import os
import random
import struct
import threading
import time

# G.711 mu-law: 8 kHz, 8 bit, so a 20 ms frame is 160 samples = 160 bytes.
SAMPLE_RATE = 8000
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000
PAYLOAD_TYPE_PCMU = 0
ULAW_SILENCE = 0xFF  # mu-law encodes linear zero as 0xFF

# G.711 is defined over a 14-bit magnitude, so 16-bit input is shifted down by 2 first.
# These constants and the segment search mirror the ITU reference implementation, which
# is also what the standard library's audioop.lin2ulaw produces.
_SEGMENT_ENDS = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)
_ULAW_BIAS = 0x84 >> 2
_ULAW_CLIP = 8159


def linear_to_ulaw(sample):
    """Encode one signed 16-bit PCM sample as a G.711 mu-law byte."""
    value = sample >> 2  # 16-bit to the 14-bit range G.711 is defined on

    if value < 0:
        value = -value
        mask = 0x7F  # mu-law inverts all bits; sign lives in the mask
    else:
        mask = 0xFF

    if value > _ULAW_CLIP:
        value = _ULAW_CLIP
    value += _ULAW_BIAS

    segment = 8
    for index, end in enumerate(_SEGMENT_ENDS):
        if value <= end:
            segment = index
            break

    if segment >= 8:
        return 0x7F ^ mask
    return ((segment << 4) | ((value >> (segment + 1)) & 0x0F)) ^ mask


def pcm16_to_ulaw(pcm_bytes):
    """Encode little-endian signed 16-bit PCM to mu-law."""
    count = len(pcm_bytes) // 2
    samples = struct.unpack(f"<{count}h", pcm_bytes[:count * 2])
    return bytes(linear_to_ulaw(sample) for sample in samples)


WAV_FORMAT_PCM = 1
WAV_FORMAT_ALAW = 6
WAV_FORMAT_MULAW = 7


def read_wav(path):
    """Read a RIFF/WAVE file, returning (audio_format, channels, rate, bits, data).

    The standard library's wave module only understands uncompressed PCM and raises on
    a mu-law WAV, which is exactly the format ffmpeg produces with -acodec pcm_mulaw.
    Parsing the chunks directly lets both formats be accepted.
    """
    with open(path, "rb") as handle:
        riff = handle.read(12)
        if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            raise ValueError(f"{path} is not a RIFF/WAVE file")

        fmt = None
        data = None
        while True:
            chunk_header = handle.read(8)
            if len(chunk_header) < 8:
                break
            chunk_id, size = struct.unpack("<4sI", chunk_header)
            payload = handle.read(size)
            if size % 2:
                handle.read(1)  # chunks are word-aligned
            if chunk_id == b"fmt " and len(payload) >= 16:
                fmt = struct.unpack("<HHIIHH", payload[:16])
            elif chunk_id == b"data":
                data = payload

    if fmt is None or data is None:
        raise ValueError(f"{path} is missing a fmt or data chunk")

    audio_format, channels, rate, _byte_rate, _align, bits = fmt
    return audio_format, channels, rate, bits, data


def load_ulaw(path):
    """Load an audio file and return raw 8 kHz mono mu-law bytes.

    Accepts a WAV file (16-bit PCM or mu-law) or a headerless .ul/.ulaw/.raw mu-law
    file. Resampling is out of scope: convert beforehand with, for example
        ffmpeg -i in.mp3 -ar 8000 -ac 1 -f mulaw out.ul
    """
    extension = os.path.splitext(path)[1].lower()

    if extension in (".ul", ".ulaw", ".raw", ".g711u"):
        with open(path, "rb") as handle:
            return handle.read()

    if extension != ".wav":
        raise ValueError(f"Unsupported audio format {extension!r}; "
                         "use .wav or headerless mu-law (.ul)")

    audio_format, channels, rate, bits, data = read_wav(path)

    if rate != SAMPLE_RATE or channels != 1:
        raise ValueError(
            f"{path} is {rate} Hz / {channels}ch; need {SAMPLE_RATE} Hz mono. "
            f"Convert with: ffmpeg -i {path} -ar 8000 -ac 1 -f mulaw out.ul")

    if audio_format == WAV_FORMAT_MULAW:
        return data
    if audio_format == WAV_FORMAT_ALAW:
        raise ValueError(
            f"{path} is A-law; only mu-law is supported. "
            f"Convert with: ffmpeg -i {path} -ar 8000 -ac 1 -f mulaw out.ul")
    if audio_format != WAV_FORMAT_PCM:
        raise ValueError(
            f"{path} has WAV format tag {audio_format} (not PCM or mu-law). "
            f"Convert with: ffmpeg -i {path} -ar 8000 -ac 1 -f mulaw out.ul")
    if bits != 16:
        raise ValueError(f"{path} is {bits}-bit PCM; need 16-bit")
    return pcm16_to_ulaw(data)


def split_frames(ulaw_bytes, frame_size=SAMPLES_PER_FRAME):
    """Split mu-law audio into fixed-size frames, padding the tail with silence."""
    frames = []
    for offset in range(0, len(ulaw_bytes), frame_size):
        frame = ulaw_bytes[offset:offset + frame_size]
        if len(frame) < frame_size:
            frame = frame + bytes([ULAW_SILENCE]) * (frame_size - len(frame))
        frames.append(frame)
    return frames


# ------------------------------------------------------------------------------- SDP

def parse_sdp(body):
    """Parse an SDP offer into {'address': str|None, 'media': [...]}.

    Each media entry is {'type', 'port', 'proto', 'formats', 'address', 'rtpmap'}.
    """
    session_address = None
    media = []
    current = None

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line.startswith("c="):
            parts = line[2:].split()
            address = parts[-1] if parts else None
            if current is None:
                session_address = address
            else:
                current["address"] = address
        elif line.startswith("m="):
            parts = line[2:].split()
            if len(parts) < 3:
                continue
            current = {
                "type": parts[0],
                "port": int(parts[1]) if parts[1].isdigit() else 0,
                "proto": parts[2],
                "formats": parts[3:],
                "address": None,
                "rtpmap": {},
            }
            media.append(current)
        elif line.startswith("a=rtpmap:") and current is not None:
            payload_type, _, encoding = line[len("a=rtpmap:"):].partition(" ")
            current["rtpmap"][payload_type.strip()] = encoding.strip()

    return {"address": session_address, "media": media}


def audio_target(offer):
    """Return (address, port) to send RTP to, or None when there is no usable audio."""
    for entry in offer["media"]:
        if entry["type"] != "audio" or entry["port"] == 0:
            continue
        address = entry["address"] or offer["address"]
        if not address:
            return None
        return address, entry["port"]
    return None


def offers_pcmu(offer):
    """True if the offer includes G.711 mu-law, which is the only codec we send."""
    for entry in offer["media"]:
        if entry["type"] == "audio" and str(PAYLOAD_TYPE_PCMU) in entry["formats"]:
            return True
    return False


# Hikvision-proprietary session-level SDP attributes. Its own intercom signalling always
# carries these, so they are included in case the device's stack expects them before it
# will treat the answer as valid. They are inert for any standards-compliant peer.
HIKVISION_SDP_ATTRIBUTES = (
    "a=doorFloor:0",
    "a=responseType:0",
    "a=doorType:1",
    "a=isSpecialType:0",
)


def telephone_event_payload(entry):
    """Return the payload type used for DTMF/telephone-event in a media entry, if any."""
    for payload_type, encoding in entry["rtpmap"].items():
        if encoding.lower().startswith("telephone-event"):
            return payload_type
    return None


def build_sdp_answer(local_address, audio_port, offer, hikvision_attributes=True):
    """Build an SDP answer accepting mu-law audio and rejecting every other stream.

    Every m-line in the offer must appear in the answer, in the same order. Streams we
    do not want are rejected by answering them with port 0, which is how video gets
    declined without failing the whole negotiation.
    """
    session_id = random.randint(100000, 999999)
    lines = [
        "v=0",
        f"o=- {session_id} {session_id} IN IP4 {local_address}",
        "s=Talk session",
        f"c=IN IP4 {local_address}",
        "t=0 0",
    ]
    if hikvision_attributes:
        lines.extend(HIKVISION_SDP_ATTRIBUTES)

    audio_done = False
    for entry in offer["media"]:
        if entry["type"] == "audio" and not audio_done:
            audio_done = True
            # Keep DTMF in the answer when offered; peers can expect it to survive.
            dtmf = telephone_event_payload(entry)
            formats = f"{PAYLOAD_TYPE_PCMU} {dtmf}" if dtmf else str(PAYLOAD_TYPE_PCMU)
            lines.append(f"m=audio {audio_port} RTP/AVP {formats}")
            lines.append(f"a=rtpmap:{PAYLOAD_TYPE_PCMU} PCMU/{SAMPLE_RATE}")
            if dtmf:
                lines.append(f"a=rtpmap:{dtmf} telephone-event/{SAMPLE_RATE}")
                lines.append(f"a=fmtp:{dtmf} 0-16")
            lines.append(f"a=ptime:{FRAME_MS}")
            # sendrecv rather than sendonly: some devices reject a sendonly answer.
            # We never read the inbound stream, we just let it arrive and drop it.
            lines.append("a=sendrecv")
        else:
            formats = " ".join(entry["formats"]) or "0"
            lines.append(f"m={entry['type']} 0 {entry['proto']} {formats}")

    return "\r\n".join(lines) + "\r\n"


# ------------------------------------------------------------------------------- RTP

class RtpSender(threading.Thread):
    """Streams mu-law frames as RTP at a steady 20 ms cadence, then reports completion.

    Runs on its own thread so the SIP loop stays responsive to BYE and heartbeats while
    audio is playing.
    """

    def __init__(self, sock, destination, frames, on_finish=None):
        super().__init__(daemon=True, name="rtp-sender")
        self.sock = sock
        self.destination = destination
        self.frames = frames
        self.on_finish = on_finish
        self.stop_event = threading.Event()
        self.packets_sent = 0

    def stop(self):
        """Ask the stream to end early; safe to call from another thread."""
        self.stop_event.set()

    def run(self):
        sequence = random.randint(0, 0xFFFF)
        timestamp = random.randint(0, 0xFFFFFFFF)
        ssrc = random.randint(0, 0xFFFFFFFF)
        next_send = time.monotonic()

        for index, frame in enumerate(self.frames):
            if self.stop_event.is_set():
                break
            # Marker bit marks the first packet of the talk spurt.
            marker = 0x80 if index == 0 else 0x00
            header = struct.pack("!BBHII", 0x80, PAYLOAD_TYPE_PCMU | marker,
                                 sequence, timestamp, ssrc)
            try:
                self.sock.sendto(header + frame, self.destination)
            except OSError as error:
                logging.error("RTP send failed: %s", error)
                break

            self.packets_sent += 1
            sequence = (sequence + 1) & 0xFFFF
            timestamp = (timestamp + SAMPLES_PER_FRAME) & 0xFFFFFFFF

            next_send += FRAME_MS / 1000.0
            delay = next_send - time.monotonic()
            if delay > 0:
                # Interruptible sleep, so stop() takes effect promptly.
                self.stop_event.wait(delay)

        logging.info("RTP stream finished after %d packets (%.1fs)",
                     self.packets_sent, self.packets_sent * FRAME_MS / 1000.0)
        if self.on_finish:
            self.on_finish(self.stop_event.is_set())
