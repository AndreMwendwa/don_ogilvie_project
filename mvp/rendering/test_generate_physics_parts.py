import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate_physics_parts as generator


class ReviewedManifestTests(unittest.TestCase):
    def make_job(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "source" / "images").mkdir(parents=True)
        (root / "source" / "images" / "0001.png").write_bytes(
            generator.PNG_SIGNATURE + b"valid-test-image"
        )
        manifest = {
            "schema_version": 1,
            "source_assets": [{
                "asset_id": "img_0001",
                "path": "source/images/0001.png",
                "mime_type": "image/png",
                "original_name": "question.png",
            }],
            "questions": [{
                "id": "q7",
                "problem": "A 2 kg block is at rest.",
                "source_assets": ["img_0001"],
                "source_regions": [{
                    "asset_id": "img_0001",
                    "bbox_normalized": [0.1, 0.1, 0.8, 0.4],
                }],
                "notes": "",
            }],
        }
        return temporary, root, manifest

    def test_arbitrary_question_id_and_region_are_valid(self):
        temporary, root, manifest = self.make_job()
        self.addCleanup(temporary.cleanup)
        validated = generator.validate_reviewed_manifest(manifest, root)
        self.assertEqual(validated["questions"][0]["id"], "q7")

    def test_missing_referenced_image_fails(self):
        temporary, root, manifest = self.make_job()
        self.addCleanup(temporary.cleanup)
        (root / "source" / "images" / "0001.png").unlink()
        with self.assertRaisesRegex(ValueError, "Missing referenced source image"):
            generator.validate_reviewed_manifest(manifest, root)

    def test_region_outside_normalized_bounds_fails(self):
        temporary, root, manifest = self.make_job()
        self.addCleanup(temporary.cleanup)
        manifest["questions"][0]["source_regions"][0]["bbox_normalized"] = [
            0.5, 0.5, 0.6, 0.6,
        ]
        with self.assertRaisesRegex(ValueError, "outside normalized image bounds"):
            generator.validate_reviewed_manifest(manifest, root)


if __name__ == "__main__":
    unittest.main()
