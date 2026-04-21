from pathlib import Path


def test_all_direct_funnel_event_inserts_set_selected_language():
    repo_root = Path(__file__).resolve().parents[1]
    files_to_check = [
        repo_root / "app" / "api" / "auth.py",
        repo_root / "app" / "api" / "billing.py",
    ]

    for file_path in files_to_check:
        source = file_path.read_text(encoding="utf-8")
        insert_blocks = source.split("insert(FunnelEvent).values(")[1:]
        assert insert_blocks, f"No FunnelEvent insert blocks found in {file_path}"
        for block in insert_blocks:
            # stop at the first closing parenthesis of .values(...)
            values_block = block.split(")\n", 1)[0]
            assert "selected_language=" in values_block, (
                f"Missing selected_language in FunnelEvent insert within {file_path}"
            )


def test_routes_funnel_payload_sets_selected_language():
    repo_root = Path(__file__).resolve().parents[1]
    routes_file = repo_root / "app" / "api" / "routes.py"
    source = routes_file.read_text(encoding="utf-8")

    assert '"selected_language": request.cookies.get("dd_lang") or "en"' in source
