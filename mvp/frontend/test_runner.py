import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import job_engine
import runner


class RunnerPhaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.job_root = Path(temporary.name) / 'jobs'
        root_patch = patch.object(job_engine, 'JOB_ROOT', self.job_root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        runner.job_dir = job_engine.job_dir
        runner.read_job = job_engine.read_job
        runner.set_status = job_engine.set_status

    def create_image_job(self):
        jid = 'a' * 32
        root = self.job_root / jid
        (root / 'source' / 'images').mkdir(parents=True)
        (root / 'input').mkdir()
        (root / 'videos').mkdir()
        (root / 'logs').mkdir()
        (root / 'source' / 'images' / '0001.png').write_bytes(b'png')
        job_engine.atomic_json(root / 'status.json', {
            'id': jid,
            'input_kind': 'image',
            'parts': [],
            'model': 'provider/model',
            'quality': 'm',
            'stages': 'yaml',
            'status': 'queued',
            'stage': 'Waiting',
            'review_status': 'pending',
            'source_assets': [{
                'asset_id': 'img_0001',
                'path': 'source/images/0001.png',
                'mime_type': 'image/png',
                'original_name': 'worksheet.png',
            }],
        })
        return jid, root

    def test_extraction_writes_manifest_and_moves_to_review(self):
        jid, root = self.create_image_job()

        def run_extractor(cmd, **kwargs):
            (root / 'input' / 'recognized_questions.json').write_text(
                json.dumps({'schema_version': 1, 'source_assets': [],
                            'questions': [{'id': 'q1'}]}),
                encoding='utf-8',
            )
            return SimpleNamespace(returncode=0)

        with patch.object(Path, 'is_file', return_value=True):
            with patch.object(runner.subprocess, 'run', side_effect=run_extractor):
                runner.main(jid, 'extract')

        job = job_engine.read_job(jid)
        self.assertEqual(job['status'], 'awaiting_review')
        self.assertEqual(job['review_status'], 'pending')
        self.assertTrue((root / 'input' / 'recognized_questions.json').is_file())

    def test_generation_requires_approval(self):
        jid, _ = self.create_image_job()
        runner.main(jid, 'generate')
        job = job_engine.read_job(jid)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('approval', job['error'])

    def test_generation_uses_reviewed_manifest_and_validate_output(self):
        jid, root = self.create_image_job()
        job_engine.set_status(jid, status='awaiting_review', review_status='approved')
        (root / 'input' / 'reviewed_questions.json').write_text(
            json.dumps({'schema_version': 1, 'source_assets': [],
                        'questions': [{'id': 'q1'}]}),
            encoding='utf-8',
        )

        def run_generator(cmd, **kwargs):
            (root / 'input' / 'reports').mkdir(exist_ok=True)
            (root / 'input' / 'reports' / 'part_01.json').write_text(
                json.dumps({'status': 'passed_all'}), encoding='utf-8')
            (root / 'input' / 'part_01_animations.yaml').write_text('questions: {}',
                                                                     encoding='utf-8')
            return SimpleNamespace(returncode=0)

        with patch.object(Path, 'is_file', return_value=True):
            with patch.object(runner.subprocess, 'run', side_effect=run_generator):
                runner.main(jid, 'generate')

        job = job_engine.read_job(jid)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['completed_parts'], [1])


if __name__ == '__main__':
    unittest.main()
