import copy
import json
from pathlib import Path

from notion_sync.incremental_ingest import ingest
from notion_sync.index_pipeline import promote


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "initial_seed.json"


def build_canonical_runtime(temp_root):
    """Build a content-eligible test runtime from the repository seed fixture."""
    temp_root = Path(temp_root)
    snapshot = copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))
    for document in snapshot["documents"]:
        if document["kind"] != "page":
            continue
        document["content_scope"] = "canonical_full_page"
        document["content"] = (
            f'<page url="{document["url"]}">\n'
            f'<content>\n{document["content"]}\n</content>\n'
            "</page>"
        )

    seed_path = temp_root / "canonical_test_seed.json"
    seed_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    runtime = temp_root / "runtime"
    ingest(seed_path, runtime)
    result = promote(runtime, mode="CANONICAL_FULL_INDEX")
    if result["status"] != "promoted" or not result["canonical_content_eligible"]:
        raise AssertionError(f"canonical test runtime was not promoted: {result}")
    return runtime
