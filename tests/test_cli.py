import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from gmdss_dsc.cli import main
from gmdss_dsc.modem import write_wav


class CLITests(unittest.TestCase):
    def test_encode_then_decode_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/"call.wav")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["encode-test",path,"--source","215322000",
                             "--destination","004634060","--centre","975"])
            self.assertEqual(code,0)
            self.assertEqual(json.loads(output.getvalue())["ecc"],85)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["decode",path])
            record = json.loads(output.getvalue())[0]
            self.assertEqual(code,0)
            self.assertEqual(record["test_call"]["source"],"215322000")
            self.assertEqual(record["test_call"]["destination"],"004634060")
            self.assertTrue(record["ecc_passed"])

    def test_no_call_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/"empty.wav")
            write_wav(path,[0]*8000,8000)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["decode",path])
            self.assertEqual(code,1)
            self.assertEqual(json.loads(output.getvalue()),[])

    def test_missing_file_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(["decode",str(Path(tmp)/"absent.wav")])
            self.assertEqual(error.exception.code,2)
