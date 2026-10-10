import io
import plistlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from asitop import utils


class TrackedFile(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.bytes_read = 0

    def read(self, size=-1):
        data = super().read(size)
        self.bytes_read += len(data)
        return data


class PowermetricsTests(unittest.TestCase):
    def sample(self, timestamp, padding=0):
        return plistlib.dumps({'timestamp': timestamp, 'padding': 'x' * padding})

    def parse(self, data):
        stream = TrackedFile(data)
        with patch('builtins.open', return_value=stream), \
             patch.object(utils, 'parse_cpu_metrics', return_value='cpu'), \
             patch.object(utils, 'parse_gpu_metrics', return_value='gpu'), \
             patch.object(utils, 'parse_thermal_pressure', return_value='thermal'):
            result = utils.parse_powermetrics()
        return result, stream.bytes_read

    def test_single_sample(self):
        self.assertEqual(self.parse(self.sample('latest'))[0],
                         ('cpu', 'gpu', 'thermal', None, 'latest'))

    def test_latest_sample(self):
        result, _ = self.parse(self.sample('old') + b'\0' + self.sample('latest'))
        self.assertEqual(result[-1], 'latest')

    def test_partial_sample_falls_back(self):
        result, _ = self.parse(self.sample('old') + b'\0' + self.sample('latest') +
                               b'\0' + self.sample('partial')[:100])
        self.assertEqual(result[-1], 'latest')

    def test_trailing_separator(self):
        result, _ = self.parse(self.sample('old') + b'\0' + self.sample('latest') + b'\0')
        self.assertEqual(result[-1], 'latest')

    def test_corrupt_latest_sample_falls_back(self):
        result, _ = self.parse(self.sample('latest') + b'\0invalid plist')
        self.assertEqual(result[-1], 'latest')

    def test_no_valid_recent_sample(self):
        self.assertFalse(self.parse(b'invalid\0also invalid')[0])

    def test_empty_file(self):
        self.assertFalse(self.parse(b'')[0])

    def test_missing_file(self):
        with patch('builtins.open', side_effect=FileNotFoundError):
            self.assertFalse(utils.parse_powermetrics())

    def test_short_files_without_separators(self):
        for data in (b'x', b'<?xml', self.sample('partial')[:100]):
            with self.subTest(size=len(data)):
                result, size = self.parse(data)
                self.assertFalse(result)
                self.assertEqual(size, len(data))

    def test_separator_only_files(self):
        for data in (b'\0', b'\0\0', b'\0' * 65537):
            with self.subTest(size=len(data)):
                self.assertFalse(self.parse(data)[0])

    def test_single_sample_with_one_separator(self):
        for data in (b'\0' + self.sample('latest'), self.sample('latest') + b'\0'):
            with self.subTest(leading=data.startswith(b'\0')):
                self.assertEqual(self.parse(data)[0][-1], 'latest')

    def test_large_single_sample_without_separators(self):
        data = self.sample('latest', 200000)
        result, size = self.parse(data)
        self.assertEqual(result[-1], 'latest')
        self.assertEqual(size, len(data))

    def test_large_invalid_file_without_separators(self):
        data = b'x' * 200000
        result, size = self.parse(data)
        self.assertFalse(result)
        self.assertEqual(size, len(data))

    def test_read_boundaries(self):
        sample = self.sample('latest')
        for length in (65535, 65536, 65537, 131071, 131072, 131073):
            with self.subTest(length=length):
                data = self.sample('old') + b'\0' + sample
                data += b' ' * (length - len(data))
                self.assertEqual(self.parse(data)[0][-1], 'latest')

    def test_separator_at_chunk_boundary(self):
        for tail_size in (65535, 65536, 65537):
            for partial in (False, True):
                with self.subTest(tail_size=tail_size, partial=partial):
                    tail = b'x' * tail_size if partial else self.sample('latest')
                    if not partial:
                        tail += b' ' * (tail_size - len(tail))
                    data = self.sample('previous') + b'\0' + tail
                    expected = 'previous' if partial else 'latest'
                    self.assertEqual(self.parse(data)[0][-1], expected)

    def test_missing_required_field_falls_back(self):
        invalid = plistlib.dumps({'padding': ''})
        result, _ = self.parse(self.sample('latest') + b'\0' + invalid)
        self.assertEqual(result[-1], 'latest')

    def test_permission_error(self):
        with patch('builtins.open', side_effect=PermissionError):
            self.assertFalse(utils.parse_powermetrics())

    def test_non_oserror_open_failure(self):
        with patch('builtins.open', side_effect=ValueError('closed file')):
            self.assertFalse(utils.parse_powermetrics())

    def test_seek_and_read_failures(self):
        for operation in ('seek', 'read'):
            with self.subTest(operation=operation):
                stream = TrackedFile(self.sample('latest'))
                with patch('builtins.open', return_value=stream), \
                     patch.object(stream, operation, side_effect=ValueError('closed file')):
                    self.assertFalse(utils.parse_powermetrics())

    def test_metrics_parser_failure(self):
        stream = TrackedFile(self.sample('latest'))
        with patch('builtins.open', return_value=stream), \
             patch.object(utils, 'parse_thermal_pressure', side_effect=KeyError('thermal')):
            self.assertFalse(utils.parse_powermetrics())

    def test_real_file_and_timecode(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = str(Path(directory) / 'powermetrics')
            Path(prefix + '123').write_bytes(self.sample('latest') + b'\0<?xml')
            with patch.object(utils, 'parse_cpu_metrics', return_value='cpu'), \
                 patch.object(utils, 'parse_gpu_metrics', return_value='gpu'), \
                 patch.object(utils, 'parse_thermal_pressure', return_value='thermal'):
                result = utils.parse_powermetrics(path=prefix, timecode='123')
            self.assertEqual(result[-1], 'latest')

    def test_history_is_not_read(self):
        data = (self.sample('old') + b'\0') * 20000 + self.sample('latest')
        result, size = self.parse(data)
        self.assertEqual(result[-1], 'latest')
        self.assertLessEqual(size, 65536)

    def test_samples_larger_than_chunk(self):
        data = (self.sample('old') + b'\0') * 20000
        data += self.sample('latest', 150000) + b'\0' + self.sample('partial')[:100]
        result, size = self.parse(data)
        self.assertEqual(result[-1], 'latest')
        self.assertLessEqual(size, 3 * 65536)


if __name__ == '__main__':
    unittest.main()
