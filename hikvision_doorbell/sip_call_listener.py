"""SIP listener that reports Hikvision doorbell rings to Home Assistant over MQTT.

Two modes, chosen by whether ANSWER_AUDIO_FILE is set:

    decline (default)  Ring for RING_HOLD_SECONDS so the visitor hears ringback, then
                       decline. No media is ever negotiated.
    answer             Answer the call, play an audio announcement to the visitor over
                       RTP, then hang up. Audio only; any video stream is declined.

Availability is driven by the door station's own heartbeat. It sends a short proprietary
datagram ("jaK") every ~20s; if those stop arriving the entity is marked unavailable in
Home Assistant, so a dead door station is visible rather than silently frozen.

Set LOG_LEVEL=DEBUG to dump every SIP message verbatim.
"""

import json
import logging
import os
import random
import select
import socket
import string
import time

import paho.mqtt.client as mqtt

import sip_audio

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")

MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
# Used to decide which source address counts as the door station's heartbeat, and to
# work out which local interface address to advertise in SDP.
DOORBELL_HOST = os.getenv("HIKVISION_DOORBELL_HOST", "")

SIP_BIND_HOST = os.getenv("SIP_BIND_HOST", "0.0.0.0")
SIP_BIND_PORT = int(os.getenv("SIP_BIND_PORT", "5060"))
# Address put in Contact and SDP, i.e. where the door station should send ACK and RTP.
# Only needed when the address we can detect locally is not the one the door station can
# reach us on, which is the case in a bridge-networked container. Prefer host networking.
SIP_ADVERTISED_HOST = os.getenv("SIP_ADVERTISED_HOST", "")
# 603 Decline = rejected outright. 486 Busy Here is the other sensible choice.
SIP_DECLINE_STATUS = os.getenv("SIP_DECLINE_STATUS", "603 Decline")
SIP_REGISTER_EXPIRES = os.getenv("SIP_REGISTER_EXPIRES", "3600")

# decline mode: how long to ring before declining.
RING_HOLD_SECONDS = float(os.getenv("RING_HOLD_SECONDS", "20"))
# The station heartbeats about every 20s, so this tolerates three missed beats.
HEARTBEAT_TIMEOUT_SECONDS = float(os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "70"))

# answer mode: set this to enable answering. 8 kHz mono .wav, or headerless mu-law .ul
ANSWER_AUDIO_FILE = os.getenv("ANSWER_AUDIO_FILE", "")
# Ring briefly before picking up, so the visitor is not answered mid-press.
ANSWER_DELAY_SECONDS = float(os.getenv("ANSWER_DELAY_SECONDS", "2"))
RTP_PORT = int(os.getenv("RTP_PORT", "40000"))
# Safety net: never hold a call open longer than this, whatever the audio length.
MAX_CALL_SECONDS = float(os.getenv("MAX_CALL_SECONDS", "60"))
# How long to wait for the ACK that confirms our 200 OK was accepted.
ACK_TIMEOUT_SECONDS = 1.0
ACK_MAX_RETRIES = 3

TOPIC_RING_STATE = "homeassistant/binary_sensor/doorbell/ring/state"
TOPIC_RING_ATTRS = "homeassistant/binary_sensor/doorbell/ring/attributes"
TOPIC_RING_CONFIG = "homeassistant/binary_sensor/doorbell/ring/config"
TOPIC_AVAILABILITY = "homeassistant/doorbell/availability"

PAYLOAD_ON = "ON"
PAYLOAD_OFF = "OFF"
PAYLOAD_ONLINE = "online"
PAYLOAD_OFFLINE = "offline"

TICK_SECONDS = 0.25
MAX_DATAGRAM = 65535

DEVICE_INFO = {
    "identifiers": ["doorbell"],
    "manufacturer": "Hikvision",
    "model": "DS-KV6113-WPE1(C)",
    "name": "Hikvision Doorbell",
    "serial_number": "12345678",
}

if not all([MQTT_HOST, MQTT_USER, MQTT_PASSWORD]):
    logging.error("MQTT_HOST, MQTT_USER and MQTT_PASSWORD must all be set.")
    raise SystemExit(1)

# SIP allows single-letter compact header forms; normalise them so parsing is uniform.
COMPACT_HEADERS = {
    "v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact",
    "l": "content-length", "c": "content-type", "s": "subject", "k": "supported",
    "e": "content-encoding", "x": "session-expires", "o": "event", "r": "refer-to",
}
CANONICAL_HEADERS = {
    "via": "Via", "from": "From", "to": "To", "call-id": "Call-ID",
    "cseq": "CSeq", "contact": "Contact",
}


# --------------------------------------------------------------------------- SIP helpers

def random_tag(length=10):
    """Return a random token suitable for a SIP tag parameter."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def parse_message(data):
    """Split a raw SIP datagram into (start_line, headers, body).

    headers maps a lower-cased header name to the list of values seen for it, because
    headers such as Via legitimately repeat and their order must be preserved.
    """
    text = data.decode("utf-8", errors="replace")
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    start_line = lines[0].strip() if lines else ""

    headers = {}
    for line in lines[1:]:
        if ":" not in line:
            continue  # ignore folded continuations and junk; not needed for this purpose
        name, _, value = line.partition(":")
        name = name.strip().lower()
        name = COMPACT_HEADERS.get(name, name)
        headers.setdefault(name, []).append(value.strip())
    return start_line, headers, body


def is_sip_request(start_line):
    """True if the start line is a real SIP request line: 'METHOD uri SIP/2.0'.

    Guards against answering the short proprietary heartbeats the door station emits,
    which would otherwise be parsed as a bogus method and draw a 501 reply.
    """
    parts = start_line.split(" ")
    return len(parts) == 3 and parts[2].upper().startswith("SIP/")


def header(headers, name, default=""):
    """Return the first value of a header, or default."""
    values = headers.get(name, [])
    return values[0] if values else default


def build_response(status_line, headers, local_tag, extra_headers=(), body=""):
    """Build a SIP response echoing the headers the transaction requires.

    Via, From, Call-ID and CSeq must come back unchanged for the sender to match the
    response to its request. To gets a tag added if the request did not carry one.
    """
    lines = ["SIP/2.0 " + status_line]

    for name in ("via", "from"):
        for value in headers.get(name, []):
            lines.append(f"{CANONICAL_HEADERS[name]}: {value}")

    to_values = headers.get("to", [])
    if to_values:
        to_value = to_values[0]
        if "tag=" not in to_value:
            to_value = f"{to_value};tag={local_tag}"
        lines.append(f"To: {to_value}")

    for name in ("call-id", "cseq"):
        for value in headers.get(name, []):
            lines.append(f"{CANONICAL_HEADERS[name]}: {value}")

    lines.extend(extra_headers)
    encoded_body = body.encode("utf-8")
    lines.append(f"Content-Length: {len(encoded_body)}")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
    return head + encoded_body


def strip_uri(contact):
    """Extract a bare SIP URI from a Contact/To style header value."""
    if "<" in contact and ">" in contact:
        return contact[contact.index("<") + 1:contact.index(">")]
    return contact.split(";")[0].strip()


def uri_user(header_value):
    """Extract the user part of a SIP URI, e.g. 'Door <sip:1@host:5060>' -> '1'."""
    uri = strip_uri(header_value)
    if uri.startswith("sip:"):
        uri = uri[4:]
    if "@" not in uri:
        return ""
    return uri.split("@", 1)[0]


def local_contact(headers, local_address):
    """Build our Contact URI, preserving the user the caller addressed.

    The ACK for a 2xx is a fresh transaction routed to this URI rather than back down
    the Via, so it has to be something the door station can actually address. A userless
    'sip:host:port' is valid SIP but not all stacks will route an ACK to it.
    """
    user = uri_user(header(headers, "to"))
    authority = f"{local_address}:{SIP_BIND_PORT}"
    return f"<sip:{user}@{authority}>" if user else f"<sip:{authority}>"


class LoggingSocket:
    """Thin socket wrapper that logs every outbound datagram at DEBUG.

    Wrapping rather than editing each call site keeps the handlers uncluttered, and
    seeing exactly what we emit is the only way to debug a picky peer.
    """

    def __init__(self, sock):
        self._sock = sock

    def sendto(self, data, addr):
        logging.debug(">>> %s:%s\n%s", addr[0], addr[1],
                      data.decode("utf-8", errors="replace"))
        return self._sock.sendto(data, addr)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def describe_caller(headers):
    """Build a human-readable summary of who is calling."""
    return (f"from={header(headers, 'from', '<unknown>')!r} "
            f"to={header(headers, 'to', '<unknown>')!r} "
            f"call-id={header(headers, 'call-id', '<none>')}")


def detect_local_address(peer):
    """Find the local address the door station would reach us on."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((peer or "8.8.8.8", 9))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def same_subnet(first, second):
    """Crude same-/24 test, enough to spot a container address vs a LAN address."""
    if not first or not second:
        return True  # nothing to compare, do not cry wolf
    return first.split(".")[:3] == second.split(".")[:3]


def resolve_advertised_address():
    """Decide which address to put in Contact and SDP, and warn when it looks wrong.

    SIP carries addresses inside its payload, so NAT breaks it in a way port mapping
    cannot fix: the peer is told to send ACK and RTP to whatever we advertise. In a
    bridge-networked container that is the container IP, which the door station cannot
    route to, and the call dies waiting for an ACK that was sent into the void.
    """
    if SIP_ADVERTISED_HOST:
        logging.info("Advertising %s in Contact/SDP (SIP_ADVERTISED_HOST)",
                     SIP_ADVERTISED_HOST)
        return SIP_ADVERTISED_HOST

    detected = detect_local_address(DOORBELL_HOST)
    if detected and DOORBELL_HOST and not same_subnet(detected, DOORBELL_HOST):
        logging.warning(
            "Local address %s is not on the same subnet as the door station %s. "
            "If this is a bridge-networked container, the door station cannot reach %s, "
            "so it will never send ACK or RTP. Use host networking, or set "
            "SIP_ADVERTISED_HOST to the address the door station can reach.",
            detected, DOORBELL_HOST, detected)
    return detected


# -------------------------------------------------------------------------------- MQTT

def setup_mqtt():
    """Connect to the broker, with an offline last will on the availability topic."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    # If this process dies the broker publishes 'offline' for us.
    client.will_set(TOPIC_AVAILABILITY, PAYLOAD_OFFLINE, qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    try:
        client.connect(MQTT_HOST)
    except Exception as error:
        logging.error("Failed to connect to MQTT broker: %s", error)
        raise SystemExit(1)
    client.loop_start()  # background thread handles reconnects
    logging.info("Connected to MQTT broker at %s", MQTT_HOST)
    return client


def publish(client, topic, payload, retain=True):
    """Publish without blocking; paho queues and retries in its network thread."""
    try:
        client.publish(topic, payload, qos=1, retain=retain)
    except Exception as error:
        logging.error("Failed to publish to %s: %s", topic, error)


def publish_discovery(client):
    """Announce the ring entity, so it survives a broker losing retained messages."""
    config = {
        "device": DEVICE_INFO,
        "name": "Ring",
        "object_id": "doorbell_ring",
        "unique_id": "doorbell_ring",
        "icon": "mdi:bell-ring",
        "state_topic": TOPIC_RING_STATE,
        "payload_on": PAYLOAD_ON,
        "payload_off": PAYLOAD_OFF,
        "json_attributes_topic": TOPIC_RING_ATTRS,
        "availability_topic": TOPIC_AVAILABILITY,
        "payload_available": PAYLOAD_ONLINE,
        "payload_not_available": PAYLOAD_OFFLINE,
        "enabled_by_default": True,
    }
    publish(client, TOPIC_RING_CONFIG, json.dumps(config))
    logging.info("Published MQTT discovery config to %s", TOPIC_RING_CONFIG)


# -------------------------------------------------------------------------------- calls

class Call:
    """One INVITE transaction and, in answer mode, the dialog and media that follow."""

    def __init__(self, call_id, headers, addr, local_tag, body):
        self.call_id = call_id
        self.headers = headers
        self.addr = addr
        self.local_tag = local_tag
        self.body = body

        self.decline_at = None      # decline mode: when to give up
        self.answer_at = None       # answer mode: when to send 200 OK
        self.answer_sent_at = None
        self.answer_retries = 0
        self.acked = False
        self.hard_deadline = None

        self.sdp_answer = None
        self.rtp_sock = None
        self.rtp_target = None
        self.sender = None
        self.stream_done = False    # set from the RTP thread, acted on by the main loop

    def close_media(self):
        """Stop streaming and release the RTP socket."""
        if self.sender:
            self.sender.stop()
            self.sender = None
        if self.rtp_sock:
            try:
                self.rtp_sock.close()
            except OSError:
                pass
            self.rtp_sock = None


class DoorbellState:
    """Tracks ring transactions and door station availability."""

    def __init__(self, client, audio_frames, local_address):
        self.client = client
        self.audio_frames = audio_frames
        self.local_address = local_address
        self.calls = {}
        self.last_seen = None     # monotonic time of last datagram from the station
        self.available = None     # None until first published, then True/False
        self.ringing = False
        self.cseq = random.randint(1, 1000)

    @property
    def answer_mode(self):
        """True when an audio announcement is configured."""
        return bool(self.audio_frames)

    def next_cseq(self):
        """Allocate a CSeq number for a request we originate (BYE)."""
        self.cseq += 1
        return self.cseq

    # --- availability ----------------------------------------------------------

    def set_available(self, available):
        """Publish availability only on change."""
        if self.available is available:
            return
        self.available = available
        payload = PAYLOAD_ONLINE if available else PAYLOAD_OFFLINE
        publish(self.client, TOPIC_AVAILABILITY, payload)
        logging.info("Door station is %s", payload)

    def note_heartbeat(self, now):
        """Record that the station is alive; any datagram from it counts."""
        self.last_seen = now
        self.set_available(True)

    def check_heartbeat(self, now):
        """Mark the station unavailable when its heartbeat stops."""
        if self.last_seen is None:
            return
        if now - self.last_seen > HEARTBEAT_TIMEOUT_SECONDS and self.available:
            logging.warning("No heartbeat for %.0fs - marking unavailable",
                            now - self.last_seen)
            self.set_available(False)

    # --- ring ------------------------------------------------------------------

    def set_ringing(self, ringing, headers=None):
        """Publish the ring state, plus caller attributes when a ring starts."""
        if ringing and headers is not None:
            attributes = {
                "caller": header(headers, "from"),
                "called": header(headers, "to"),
                "call_id": header(headers, "call-id"),
                "subject": header(headers, "subject"),
                "user_agent": header(headers, "user-agent"),
                "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            publish(self.client, TOPIC_RING_ATTRS, json.dumps(attributes))
        if self.ringing is ringing:
            return
        self.ringing = ringing
        publish(self.client, TOPIC_RING_STATE, PAYLOAD_ON if ringing else PAYLOAD_OFF)
        logging.info("Ring -> %s", PAYLOAD_ON if ringing else PAYLOAD_OFF)

    def refresh_ringing(self):
        """Ring is on for as long as at least one call is pending."""
        self.set_ringing(bool(self.calls))

    def drop_call(self, call):
        """Forget a call and release anything it held."""
        call.close_media()
        self.calls.pop(call.call_id, None)
        self.refresh_ringing()


# ---------------------------------------------------------------------------- handlers

def open_rtp_socket():
    """Bind the RTP socket, preferring the configured port and falling back to any."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", RTP_PORT))
    except OSError:
        logging.warning("RTP port %s unavailable, using an ephemeral port", RTP_PORT)
        sock.bind(("0.0.0.0", 0))
    return sock


def handle_invite(sock, addr, headers, body, state, now):
    """Report the ring, then either hold-and-decline or prepare to answer."""
    call_id = header(headers, "call-id")

    if call_id in state.calls:
        # Retransmission of an INVITE we already hold: re-send the provisional.
        logging.debug("Duplicate INVITE for %s, re-sending 180", call_id)
        sock.sendto(build_response("180 Ringing", headers,
                                   state.calls[call_id].local_tag), addr)
        return

    logging.info("INCOMING CALL  %s", describe_caller(headers))
    call = Call(call_id, headers, addr, random_tag(), body)
    state.calls[call_id] = call

    # 100 Trying stops INVITE retransmissions; 180 gives the visitor ringback.
    sock.sendto(build_response("100 Trying", headers, call.local_tag), addr)
    sock.sendto(build_response("180 Ringing", headers, call.local_tag), addr)
    state.set_ringing(True, headers)

    if not state.answer_mode:
        call.decline_at = now + RING_HOLD_SECONDS
        logging.info("Ringing for %.0fs before declining", RING_HOLD_SECONDS)
        return

    # Answer mode: validate the offer before committing to pick up.
    offer = sip_audio.parse_sdp(body)
    target = sip_audio.audio_target(offer)
    if not target or not sip_audio.offers_pcmu(offer):
        logging.error("Offer has no usable G.711 mu-law audio; declining instead. "
                      "media=%s", [(m["type"], m["port"], m["formats"])
                                   for m in offer["media"]])
        call.decline_at = now
        return

    call.rtp_target = target
    call.rtp_sock = open_rtp_socket()
    local_rtp_port = call.rtp_sock.getsockname()[1]
    call.sdp_answer = sip_audio.build_sdp_answer(state.local_address,
                                                local_rtp_port, offer)
    call.answer_at = now + ANSWER_DELAY_SECONDS
    logging.info("Will answer in %.1fs: audio -> %s:%s, our RTP port %s",
                 ANSWER_DELAY_SECONDS, target[0], target[1], local_rtp_port)


def send_answer(sock, call, state, now):
    """Send 200 OK carrying our SDP answer."""
    extra = [
        f"Contact: {local_contact(call.headers, state.local_address)}",
        "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, MESSAGE, INFO",
        "Content-Type: application/sdp",
    ]
    sock.sendto(build_response("200 OK", call.headers, call.local_tag,
                               extra, call.sdp_answer), call.addr)
    call.answer_sent_at = now
    call.answer_retries += 1
    call.answer_at = None
    logging.info("Answered call %s (200 OK with SDP, attempt %d)",
                 call.call_id, call.answer_retries)


def start_streaming(call, state):
    """Begin the RTP announcement once the caller has acknowledged our answer."""
    def on_finish(stopped_early):
        # Runs on the RTP thread; the main loop turns this into a BYE.
        call.stream_done = True
        if stopped_early:
            logging.debug("RTP stream for %s stopped early", call.call_id)

    call.sender = sip_audio.RtpSender(call.rtp_sock, call.rtp_target,
                                      state.audio_frames, on_finish)
    call.sender.start()
    duration = len(state.audio_frames) * sip_audio.FRAME_MS / 1000.0
    logging.info("Streaming %.1fs of audio to %s:%s",
                 duration, call.rtp_target[0], call.rtp_target[1])


def send_bye(sock, call, state):
    """Hang up a call we answered."""
    target = strip_uri(header(call.headers, "contact")) or f"sip:{call.addr[0]}"
    # In-dialog requests from the callee swap From and To.
    local_to = header(call.headers, "to")
    if "tag=" not in local_to:
        local_to = f"{local_to};tag={call.local_tag}"

    lines = [
        f"BYE {target} SIP/2.0",
        f"Via: SIP/2.0/UDP {state.local_address}:{SIP_BIND_PORT}"
        f";branch=z9hG4bK{random_tag(12)}",
        "Max-Forwards: 70",
        f"From: {local_to}",
        f"To: {header(call.headers, 'from')}",
        f"Call-ID: {call.call_id}",
        f"CSeq: {state.next_cseq()} BYE",
        "Content-Length: 0",
    ]
    sock.sendto(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"), call.addr)
    logging.info("Sent BYE for %s", call.call_id)


def handle_ack(call_id, state):
    """An ACK confirms our 200 OK; media can start."""
    call = state.calls.get(call_id)
    if not call or call.acked or not call.sdp_answer:
        logging.debug("ACK received (no pending answer to confirm)")
        return
    call.acked = True
    logging.info("ACK received for %s - call established", call_id)
    start_streaming(call, state)


def handle_cancel(sock, addr, headers, state):
    """Visitor gave up before we answered."""
    call_id = header(headers, "call-id")
    logging.info("CANCEL for %s", call_id)
    sock.sendto(build_response("200 OK", headers, random_tag()), addr)

    call = state.calls.get(call_id)
    if call:
        # The INVITE transaction still needs a final response.
        sock.sendto(build_response("487 Request Terminated",
                                   call.headers, call.local_tag), call.addr)
        state.drop_call(call)
    else:
        state.refresh_ringing()


def handle_bye(sock, addr, headers, state):
    """The door station hung up; stop any audio we were playing."""
    call_id = header(headers, "call-id")
    logging.info("BYE from door station for %s", call_id)
    sock.sendto(build_response("200 OK", headers, random_tag()), addr)
    call = state.calls.get(call_id)
    if call:
        state.drop_call(call)


def handle_message(sock, addr, headers, body):
    """Acknowledge an in-call MESSAGE.

    The door station sends one right after we answer, carrying its lock count as
    <locknumXML><lockNum>N</lockNum></locknumXML>. It must be acknowledged: answering
    501 here makes the station abandon the call instead of sending the ACK that
    establishes it.
    """
    summary = " ".join(body.split())
    logging.info("MESSAGE from door station: %s", summary or "(empty)")
    sock.sendto(build_response("200 OK", headers, random_tag()), addr)


def handle_register(sock, addr, headers):
    """Accept any registration unconditionally; credentials are never checked."""
    logging.info("REGISTER  %s", describe_caller(headers))
    extra = [f"Contact: {value}" for value in headers.get("contact", [])]
    # Honour the expiry the peer asked for, otherwise it cannot know when to refresh.
    requested = header(headers, "expires", SIP_REGISTER_EXPIRES)
    extra.append(f"Expires: {requested}")
    sock.sendto(build_response("200 OK", headers, random_tag(), extra), addr)
    logging.info("Registration accepted (expires=%s)", requested)


def handle_request(sock, addr, method, headers, body, state, now):
    """Dispatch a parsed SIP request to the right minimal handler."""
    if method == "INVITE":
        handle_invite(sock, addr, headers, body, state, now)
    elif method == "ACK":
        handle_ack(header(headers, "call-id"), state)
    elif method == "CANCEL":
        handle_cancel(sock, addr, headers, state)
    elif method == "BYE":
        handle_bye(sock, addr, headers, state)
    elif method == "MESSAGE":
        handle_message(sock, addr, headers, body)
    elif method == "REGISTER":
        handle_register(sock, addr, headers)
    elif method in ("OPTIONS", "INFO", "NOTIFY", "SUBSCRIBE"):
        logging.info("%s -> 200 OK", method)
        sock.sendto(build_response("200 OK", headers, random_tag()), addr)
    else:
        # Acknowledge rather than reject. This is a closed appliance stack that reacts
        # to a 4xx/5xx by tearing the call down, so a stricter answer costs us the call.
        logging.warning("Unhandled method %s -> 200 OK (permissive)", method)
        sock.sendto(build_response("200 OK", headers, random_tag()), addr)


def service_calls(sock, state, now):
    """Drive call timers: answering, ACK retries, stream completion, declining."""
    for call in list(state.calls.values()):
        # decline mode, or an answer-mode call whose offer we could not use
        if call.decline_at is not None and now >= call.decline_at:
            sock.sendto(build_response(SIP_DECLINE_STATUS, call.headers,
                                       call.local_tag), call.addr)
            logging.info("Declined %s with %s", call.call_id, SIP_DECLINE_STATUS)
            state.drop_call(call)
            continue

        if call.answer_at is not None and now >= call.answer_at:
            send_answer(sock, call, state, now)
            call.hard_deadline = now + MAX_CALL_SECONDS
            continue

        # Our 200 OK may have been lost; resend until the ACK arrives.
        if (call.answer_sent_at is not None and not call.acked
                and now - call.answer_sent_at >= ACK_TIMEOUT_SECONDS):
            if call.answer_retries >= ACK_MAX_RETRIES:
                logging.error("No ACK for %s after %d attempts - giving up",
                              call.call_id, call.answer_retries)
                state.drop_call(call)
            else:
                send_answer(sock, call, state, now)
            continue

        if call.stream_done:
            logging.info("Announcement finished for %s", call.call_id)
            send_bye(sock, call, state)
            state.drop_call(call)
            continue

        if call.hard_deadline is not None and now >= call.hard_deadline:
            logging.warning("Call %s exceeded %.0fs - hanging up",
                            call.call_id, MAX_CALL_SECONDS)
            send_bye(sock, call, state)
            state.drop_call(call)


# ---------------------------------------------------------------------------- main loop

def bind_socket():
    """Bind the SIP UDP socket."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((SIP_BIND_HOST, SIP_BIND_PORT))
    except OSError as error:
        logging.error("Cannot bind %s:%s - %s", SIP_BIND_HOST, SIP_BIND_PORT, error)
        raise SystemExit(1)
    return LoggingSocket(sock)


def load_audio():
    """Load and frame the announcement, or return None for decline mode."""
    if not ANSWER_AUDIO_FILE:
        return None
    try:
        ulaw = sip_audio.load_ulaw(ANSWER_AUDIO_FILE)
    except (OSError, ValueError) as error:
        logging.error("Cannot use ANSWER_AUDIO_FILE %s: %s", ANSWER_AUDIO_FILE, error)
        raise SystemExit(1)
    frames = sip_audio.split_frames(ulaw)
    logging.info("Loaded %s: %.1fs of audio (%d frames)", ANSWER_AUDIO_FILE,
                 len(frames) * sip_audio.FRAME_MS / 1000.0, len(frames))
    return frames


def process_datagram(sock, addr, data, state, now):
    """Classify a datagram and dispatch it."""
    start_line, headers, body = parse_message(data)

    if start_line.startswith("SIP/2.0"):
        logging.info("Response from %s: %s", addr[0], start_line)
        return

    if not is_sip_request(start_line):
        # The door station's proprietary heartbeat. Counts as liveness, never answered.
        logging.debug("Heartbeat / non-SIP datagram from %s:%s (%d bytes, %r)",
                      addr[0], addr[1], len(data), data[:16])
        return

    method = start_line.split(" ", 1)[0].upper()
    logging.debug("%s from %s:%s", method, addr[0], addr[1])
    handle_request(sock, addr, method, headers, body, state, now)


def main():
    """Listen for SIP traffic, report rings to MQTT, and track availability."""
    audio_frames = load_audio()

    local_address = resolve_advertised_address()
    if audio_frames and not local_address:
        logging.error("Cannot determine a local address to advertise in SDP; "
                      "set HIKVISION_DOORBELL_HOST so it can be derived.")
        raise SystemExit(1)

    client = setup_mqtt()
    publish_discovery(client)

    state = DoorbellState(client, audio_frames, local_address)
    # Report unavailable until the station's first heartbeat actually arrives.
    state.set_available(False)
    state.set_ringing(False)

    sock = bind_socket()
    mode = "answer with audio" if state.answer_mode else \
        f"hold {RING_HOLD_SECONDS:.0f}s then {SIP_DECLINE_STATUS}"
    logging.info("SIP listener up on %s:%s (UDP), local address %s, mode: %s",
                 SIP_BIND_HOST, SIP_BIND_PORT, local_address, mode)

    while True:
        try:
            readable, _, _ = select.select([sock], [], [], TICK_SECONDS)
        except OSError as error:
            logging.error("select failed: %s", error)
            continue

        now = time.monotonic()

        if readable:
            try:
                data, addr = sock.recvfrom(MAX_DATAGRAM)
            except OSError as error:
                logging.error("Socket read failed: %s", error)
                data, addr = b"", None

            if addr and (not DOORBELL_HOST or addr[0] == DOORBELL_HOST):
                state.note_heartbeat(now)

            if addr and data.strip():
                logging.debug("<<< %s:%s\n%s", addr[0], addr[1],
                              data.decode("utf-8", errors="replace"))
                try:
                    process_datagram(sock, addr, data, state, now)
                except Exception as error:  # never let one bad message kill the loop
                    logging.exception("Failed handling datagram from %s: %s",
                                      addr[0], error)

        service_calls(sock, state, time.monotonic())
        state.check_heartbeat(time.monotonic())


if __name__ == "__main__":
    main()
