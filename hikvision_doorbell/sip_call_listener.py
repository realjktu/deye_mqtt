"""SIP listener that reports Hikvision doorbell rings to Home Assistant over MQTT.

This is deliberately not a SIP user agent: no media is negotiated and no call is ever
answered. When the door station sends an INVITE (someone pressed the button) the listener
reports the ring to MQTT, keeps the call ringing for a fixed hold time so the visitor
hears ringback, and then declines it.

Availability is driven by the door station's own heartbeat. It sends a short proprietary
datagram ("jaK") every ~20s; if those stop arriving the entity is marked unavailable in
Home Assistant, so a dead door station is visible rather than silently frozen.

Handled requests:
    INVITE   -> publish ring ON, reply 100 Trying + 180 Ringing, decline after the hold
    CANCEL   -> visitor gave up: reply 200 OK, then 487 for the INVITE, publish ring OFF
    REGISTER -> reply 200 OK; credentials are never checked
    OPTIONS  -> reply 200 OK (keepalive)
    BYE      -> reply 200 OK
    ACK      -> logged only; ACK is never responded to

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

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")

MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
# Used only to decide which source address counts as the door station's heartbeat.
DOORBELL_HOST = os.getenv("HIKVISION_DOORBELL_HOST", "")

SIP_BIND_HOST = os.getenv("SIP_BIND_HOST", "0.0.0.0")
SIP_BIND_PORT = int(os.getenv("SIP_BIND_PORT", "5060"))
# 603 Decline = rejected outright. 486 Busy Here is the other sensible choice.
SIP_DECLINE_STATUS = os.getenv("SIP_DECLINE_STATUS", "603 Decline")
SIP_REGISTER_EXPIRES = os.getenv("SIP_REGISTER_EXPIRES", "3600")

# How long to keep the call ringing before declining it.
RING_HOLD_SECONDS = float(os.getenv("RING_HOLD_SECONDS", "20"))
# The station heartbeats about every 20s, so this tolerates three missed beats.
HEARTBEAT_TIMEOUT_SECONDS = float(os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "70"))

TOPIC_RING_STATE = "homeassistant/binary_sensor/doorbell/ring/state"
TOPIC_RING_ATTRS = "homeassistant/binary_sensor/doorbell/ring/attributes"
TOPIC_RING_CONFIG = "homeassistant/binary_sensor/doorbell/ring/config"
TOPIC_AVAILABILITY = "homeassistant/doorbell/availability"

PAYLOAD_ON = "ON"
PAYLOAD_OFF = "OFF"
PAYLOAD_ONLINE = "online"
PAYLOAD_OFFLINE = "offline"

TICK_SECONDS = 1.0
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


def build_response(status_line, headers, local_tag, extra_headers=()):
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
    lines.append("Content-Length: 0")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


def header(headers, name, default=""):
    """Return the first value of a header, or default."""
    values = headers.get(name, [])
    return values[0] if values else default


def describe_caller(headers):
    """Build a human-readable summary of who is calling."""
    return (f"from={header(headers, 'from', '<unknown>')!r} "
            f"to={header(headers, 'to', '<unknown>')!r} "
            f"call-id={header(headers, 'call-id', '<none>')}")


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


# ------------------------------------------------------------------------------- state

class DoorbellState:
    """Tracks ring transactions and door station availability."""

    def __init__(self, client):
        self.client = client
        self.calls = {}           # call-id -> {"headers", "addr", "tag", "deadline"}
        self.last_seen = None     # monotonic time of last datagram from the station
        self.available = None     # None until first published, then True/False
        self.ringing = False

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


# ---------------------------------------------------------------------------- handlers

def handle_invite(sock, addr, headers, state, now):
    """Report the ring and keep the call alive until the hold time expires."""
    call_id = header(headers, "call-id")

    if call_id in state.calls:
        # Retransmission of an INVITE we are already holding: re-send the provisional.
        logging.debug("Duplicate INVITE for %s, re-sending 180", call_id)
        sock.sendto(build_response("180 Ringing", headers,
                                   state.calls[call_id]["tag"]), addr)
        return

    logging.info("INCOMING CALL  %s", describe_caller(headers))
    local_tag = random_tag()
    state.calls[call_id] = {
        "headers": headers, "addr": addr, "tag": local_tag,
        "deadline": now + RING_HOLD_SECONDS,
    }
    # 100 Trying stops INVITE retransmissions; 180 gives the visitor ringback.
    sock.sendto(build_response("100 Trying", headers, local_tag), addr)
    sock.sendto(build_response("180 Ringing", headers, local_tag), addr)
    state.set_ringing(True, headers)
    logging.info("Ringing for %.0fs before declining", RING_HOLD_SECONDS)


def handle_cancel(sock, addr, headers, state):
    """Visitor gave up before the hold expired."""
    call_id = header(headers, "call-id")
    logging.info("CANCEL for %s", call_id)
    sock.sendto(build_response("200 OK", headers, random_tag()), addr)

    call = state.calls.pop(call_id, None)
    if call:
        # The INVITE transaction still needs a final response.
        sock.sendto(build_response("487 Request Terminated",
                                   call["headers"], call["tag"]), call["addr"])
    state.refresh_ringing()


def handle_register(sock, addr, headers):
    """Accept any registration unconditionally; credentials are never checked."""
    logging.info("REGISTER  %s", describe_caller(headers))
    extra = [f"Contact: {value}" for value in headers.get("contact", [])]
    # Honour the expiry the peer asked for, otherwise it cannot know when to refresh.
    requested = header(headers, "expires", SIP_REGISTER_EXPIRES)
    extra.append(f"Expires: {requested}")
    sock.sendto(build_response("200 OK", headers, random_tag(), extra), addr)
    logging.info("Registration accepted (expires=%s)", requested)


def handle_request(sock, addr, method, headers, state, now):
    """Dispatch a parsed SIP request to the right minimal handler."""
    if method == "INVITE":
        handle_invite(sock, addr, headers, state, now)
    elif method == "CANCEL":
        handle_cancel(sock, addr, headers, state)
    elif method == "REGISTER":
        handle_register(sock, addr, headers)
    elif method == "ACK":
        logging.debug("ACK received (no response required)")
    elif method in ("OPTIONS", "BYE", "INFO", "NOTIFY", "SUBSCRIBE"):
        logging.info("%s -> 200 OK", method)
        sock.sendto(build_response("200 OK", headers, random_tag()), addr)
    else:
        logging.warning("Unhandled method %s -> 501", method)
        sock.sendto(build_response("501 Not Implemented", headers, random_tag()), addr)


def expire_calls(sock, state, now):
    """Decline any call whose hold time has elapsed."""
    for call_id, call in list(state.calls.items()):
        if now < call["deadline"]:
            continue
        sock.sendto(build_response(SIP_DECLINE_STATUS, call["headers"],
                                   call["tag"]), call["addr"])
        del state.calls[call_id]
        logging.info("Hold elapsed for %s - declined with %s",
                     call_id, SIP_DECLINE_STATUS)
    state.refresh_ringing()


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
    return sock


def main():
    """Listen for SIP traffic, report rings to MQTT, and track availability."""
    client = setup_mqtt()
    publish_discovery(client)

    state = DoorbellState(client)
    # Report unavailable until the station's first heartbeat actually arrives.
    state.set_available(False)
    state.set_ringing(False)

    sock = bind_socket()
    logging.info("SIP listener up on %s:%s (UDP), hold %.0fs then %s",
                 SIP_BIND_HOST, SIP_BIND_PORT, RING_HOLD_SECONDS, SIP_DECLINE_STATUS)

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
                    logging.error("Failed handling datagram from %s: %s", addr[0], error)

        expire_calls(sock, state, time.monotonic())
        state.check_heartbeat(time.monotonic())


def process_datagram(sock, addr, data, state, now):
    """Classify a datagram and dispatch it."""
    start_line, headers, _body = parse_message(data)

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
    handle_request(sock, addr, method, headers, state, now)


if __name__ == "__main__":
    main()
