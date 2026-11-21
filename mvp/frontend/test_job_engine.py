import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import job_engine


class JobEngineImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root_patch = patch.object(job_engine, 'JOB_ROOT', Path(temporary.name) / 'jobs')
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.png = self.image_bytes('PNG')
        self.jpeg = self.image_bytes('JPEG')

    @staticmethod
    def image_bytes(image_format):
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), 'white').save(stream, format=image_format)
        return stream.getvalue()

    def make_image_job(self):
        images = [
            {'name': r'C:\temp\..\worksheet.png', 'mime_type': 'image/png', 'data': self.png},
            {'name': 'photo.jpg', 'mime_type': 'image/jpeg', 'data': self.jpeg},
        ]
        with patch.object(job_engine.subprocess, 'Popen',
                          return_value=SimpleNamespace(pid=123)) as launch:
            job = job_engine.create_image_job(images, 'provider/model', 'm', 'yaml')
        return job, launch

    def test_create_image_job_persists_safe_assets_and_starts_extraction(self):
        job, launch = self.make_image_job()

        self.assertEqual(job['input_kind'], 'image')
        self.assertEqual(job['review_status'], 'pending')
        self.assertEqual(job['filename'], 'worksheet.png')
        self.assertEqual([asset['path'] for asset in job['source_assets']], [
            'source/images/0001.png', 'source/images/0002.jpg'])
        self.assertEqual(
            (job_engine.job_dir(job['id']) / 'source' / 'images' / '0002.jpg').read_bytes(),
            self.jpeg)
        self.assertEqual(launch.call_args.args[0][-2:], [job['id'], 'extract'])

    def test_rejects_empty_malformed_unsupported_and_oversized_images(self):
        malformed = {'name': 'broken.png', 'mime_type': 'image/png', 'data': b'\x89PNG\r\n\x1a\n'}
        unsupported = {'name': 'photo.gif', 'mime_type': 'image/gif', 'data': self.png}
        with self.assertRaises(ValueError):
            job_engine._validated_image_records([])
        with self.assertRaises(ValueError):
            job_engine._validated_image_records([malformed])
        mismatched = {'name': 'photo.png', 'mime_type': 'image/png', 'data': self.jpeg}
        with self.assertRaises(ValueError):
            job_engine._validated_image_records([mismatched])
        with self.assertRaises(ValueError):
            job_engine._validated_image_records([unsupported])
        with patch.object(job_engine, 'MAX_IMAGE_FILES', 1):
            with self.assertRaises(ValueError):
                job_engine._validated_image_records([
                    {'name': 'one.png', 'mime_type': 'image/png', 'data': self.png},
                    {'name': 'two.jpg', 'mime_type': 'image/jpeg', 'data': self.jpeg},
                ])
        with patch.object(job_engine, 'MAX_IMAGE_MB', 0):
            with self.assertRaises(ValueError):
                job_engine._validated_image_records([
                    {'name': 'large.png', 'mime_type': 'image/png', 'data': self.png}])
        with patch.object(job_engine, 'MAX_IMAGE_TOTAL_MB', 0):
            with self.assertRaises(ValueError):
                job_engine._validated_image_records([
                    {'name': 'large.png', 'mime_type': 'image/png', 'data': self.png}])

    def test_review_approval_validates_assets_and_enables_generation(self):
        job, _ = self.make_image_job()
        job_engine.set_status(job['id'], status='awaiting_review', stage='Review')
        question = {
            'id': 'q1',
            'problem': 'Calculate the force.',
            'source_assets': ['img_0001'],
            'source_regions': [{
                'asset_id': 'img_0001',
                'bbox_normalized': [0.1, 0.1, 0.8, 0.8],
            }],
        }
        manifest = {
            'schema_version': 1,
            'source_assets': job['source_assets'],
            'questions': [question],
        }
        invalid = dict(manifest, questions=[dict(question, source_assets=['missing'])])
        with self.assertRaises(ValueError):
            job_engine.save_reviewed_questions(job['id'], invalid)
        with self.assertRaises(ValueError):
            job_engine.launch_image_phase(job['id'], 'generate')
        self.assertEqual(job_engine.read_job(job['id'])['review_status'], 'pending')

        approved = job_engine.save_reviewed_questions(job['id'], manifest)
        self.assertEqual(approved['review_status'], 'approved')
        reviewed_path = job_engine.job_dir(job['id']) / 'input' / 'reviewed_questions.json'
        self.assertEqual(
            json.loads(reviewed_path.read_text(encoding='utf-8'))['questions'][0]['id'],
            'q1')
        with patch.object(job_engine.subprocess, 'Popen',
                          return_value=SimpleNamespace(pid=456)) as launch:
            started = job_engine.launch_image_phase(job['id'], 'generate')
        self.assertEqual(started['status'], 'queued')
        self.assertEqual(launch.call_args.args[0][-2:], [job['id'], 'generate'])

    def test_reviewed_assets_cannot_escape_job_directory(self):
        job, _ = self.make_image_job()
        job_engine.set_status(job['id'], status='awaiting_review', stage='Review')
        unsafe_assets = [dict(job['source_assets'][0], path='source/images/../../status.json')]
        job_engine.set_status(job['id'], source_assets=unsafe_assets)
        manifest = {
            'schema_version': 1,
            'source_assets': unsafe_assets,
            'questions': [{
                'id': 'q1',
                'problem': 'Question text.',
                'source_assets': ['img_0001'],
            }],
        }

        with self.assertRaises(ValueError):
            job_engine.save_reviewed_questions(job['id'], manifest)

    def test_pdf_job_creation_remains_unchanged(self):
        with patch.object(job_engine.subprocess, 'Popen',
                          return_value=SimpleNamespace(pid=789)) as launch:
            job = job_engine.create_job(
                'book.pdf', b'%PDF-valid', '1', 'provider/model', 'm', 'yaml')

        self.assertNotIn('input_kind', job)
        self.assertEqual(
            (job_engine.job_dir(job['id']) / 'source' / 'workbook.pdf').read_bytes(),
            b'%PDF-valid')
        self.assertEqual(len(launch.call_args.args[0]), 3)

    def test_invalid_job_id_is_rejected(self):
        with self.assertRaises(ValueError):
            job_engine.job_dir('../outside')


if __name__ == '__main__':
    unittest.main()
