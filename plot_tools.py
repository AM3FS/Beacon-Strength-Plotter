"""Signal measurements, alert state and unattended plot recording."""

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4


def utc_day(timestamp):
    return int(timestamp // 86400)


def align_recording(primary_start, samples):
    offset = (utc_day(primary_start) - utc_day(samples[0][0])) * 86400
    return [(stamp + offset, raw, hz, callsign) for stamp, raw, hz, callsign in samples]


def alert_preferences(value):
    if not isinstance(value, dict):
        return {}
    return {key: rule for key, rule in value.items()
            if isinstance(rule, list) and len(rule) == 2
            and type(rule[0]) is int and -200 <= rule[0] <= 50
            and type(rule[1]) is int and 1 <= rule[1] <= 3600}


class LiftDetector:
    def __init__(self):
        self.latched = set()
        self.reset()

    def reset(self):
        self.key = None
        self.last_stamp = None
        self.since = None

    def clear(self, key=None):
        self.reset()
        if key is None:
            self.latched.clear()
        else:
            self.latched.discard(key)

    def update(self, key, stamp, level, rule, max_gap):
        if (rule is None or level is None or key != self.key or self.last_stamp is None
                or not 0 <= stamp - self.last_stamp <= max_gap):
            self.reset()
        if rule is None or level is None:
            return False
        self.key, self.last_stamp = key, stamp
        threshold, duration = rule
        if level < threshold:
            self.since = None
            if level <= threshold - 2:
                self.latched.discard(key)
            return False
        if self.since is None:
            self.since = stamp
        if key not in self.latched and stamp - self.since >= duration:
            self.latched.add(key)
            return True
        return False


class DailyRecording:
    """Write one atomic snapshot per UTC day, radio and recording session."""

    def __init__(self):
        self.session = uuid4().hex[:12]
        self.samples = defaultdict(list)
        self.comments = defaultdict(dict)
        self.dirty = set()

    def append(self, folder, model, sample):
        key = (Path(folder), model, utc_day(sample[0]))
        if sample[1] is None and key not in self.samples:
            return
        self.samples[key].append(sample)
        self.dirty.add(key)

    def comment(self, folder, model, stamp, text):
        key = (Path(folder), model, utc_day(stamp))
        samples = self.samples.get(key)
        if samples and samples[0][0] <= stamp <= samples[-1][0]:
            if text is None:
                self.comments[key].pop(stamp, None)
            else:
                self.comments[key][stamp] = text
            self.dirty.add(key)

    def redirect_pending(self, folder):
        for key in tuple(self.dirty):
            _, model, day = key
            target = folder, model, day
            if target == key:
                continue
            samples = self.samples.pop(key)
            if self.samples.get(target):
                samples = sorted(set(samples + self.samples[target]), key=lambda sample: sample[0])
            self.samples[target] = samples
            self.comments[target].update(self.comments.pop(key, {}))
            self.dirty.remove(key)
            self.dirty.add(target)

    def flush(self, current_day, plot_format):
        errors = []
        for key in sorted(self.dirty):
            folder, model, day = key
            date = datetime.fromtimestamp(day * 86400, timezone.utc).strftime('%Y-%m-%d')
            path = folder / f'Beacon-{date}-UTC-{model.replace(" ", "-")}-{self.session}.bplot'
            temp = path.with_suffix('.tmp')
            data = {'format': plot_format, 'radio_model': model, 'samples': self.samples[key],
                    'comments': sorted(self.comments[key].items())}
            try:
                folder.mkdir(parents=True, exist_ok=True)
                temp.write_text(json.dumps(data, separators=(',', ':')), encoding='utf-8')
                temp.replace(path)
            except OSError as exc:
                errors.append(str(exc))
                continue
            self.dirty.discard(key)
        for key in tuple(self.samples):
            if key[2] < current_day - 1 and key not in self.dirty:
                del self.samples[key]
                self.comments.pop(key, None)
        return errors
