"""Receive metering and confirmed CW beacon tuning for Yaesu USB CAT."""

import re
import time


YAESU_MODELS = ('Yaesu FTX-1', 'Yaesu FT-891')


def supports_frequency(model, hz):
    if model == 'Yaesu FTX-1':
        return (100_000 <= hz <= 75_000_000 or 108_000_000 <= hz <= 174_000_000
                or 400_000_000 <= hz <= 470_000_000)
    if model == 'Yaesu FT-891':
        return 1_800_000 <= hz <= 54_000_000
    return True


def open_cat_port(serial, port_name, baud):
    # Preconfigure the control lines before opening the Enhanced CAT port.
    port = serial.Serial(baudrate=baud, timeout=0.1, write_timeout=0.5)
    port.rts = False
    port.dtr = False
    port.port = port_name
    port.open()
    return port


class YaesuCAT:
    def __init__(self, port, model):
        self.port = port
        self.model = model
        self.ftx1 = model == 'Yaesu FTX-1'
        self.pending = bytearray()

    def query(self, command, pattern, timeout=0.8):
        self.port.write(command)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pending.extend(self.port.read(min(self.port.in_waiting or 1, 1024)))
            while b';' in self.pending:
                end = self.pending.index(b';')
                reply = bytes(self.pending[:end]).strip()
                del self.pending[:end + 1]
                match = re.fullmatch(pattern, reply)
                if match:
                    return match
            if len(self.pending) > 1024:
                self.pending.clear()
        return None

    def read_meter(self):
        reply = self.query(b'SM0;', rb'SM0([0-9]{3})')
        if reply:
            level = int(reply[1])
            if level <= 255:
                return level
        return None

    def read_state(self):
        # The IF response carries the receive frequency and VFO/memory state.
        channel_width = 5 if self.ftx1 else 3
        pattern = rb'IF[ -~]{%d}([0-9]{9})[+-][0-9]{4}[01]{2}([0-9A-Z])([0-9])[0-9]{4}' % channel_width
        return self.query(b'IF;', pattern, timeout=0.25)

    def read_frequency(self):
        reply = self.read_state()
        return (int(reply[1]) or None) if reply else None

    def tune_beacon(self, hz):
        if not supports_frequency(self.model, hz):
            raise ValueError('This beacon frequency is outside the radio’s supported tuning ranges.')
        state = self.read_state()
        if state is None:
            raise ValueError('Could not read the receiver’s VFO state.')
        if state[3] != b'0':
            if not self.ftx1:
                raise ValueError('Select VFO mode on the FT-891 before tuning to a beacon.')
            self.port.write(b'VM000;')
            if not self.query(b'VM0;', rb'VM000'):
                raise ValueError('The FTX-1 did not confirm Main VFO mode.')
        self.port.write(f'FA{hz:09d};'.encode('ascii'))
        self.port.write(b'MD03;')
        # Set commands do not acknowledge success. Verify their effect on the receiver.
        for _ in range(3):
            state = self.read_state()
            if state is not None and int(state[1]) == hz and state[2] == b'3' and state[3] == b'0':
                return
            time.sleep(0.1)
        raise ValueError('The radio did not confirm the beacon frequency in CW.')
