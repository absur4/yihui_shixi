"""Offline browser acceptance; extra module slots use explicitly labeled UI-only fixtures."""

import argparse
import copy
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    options = parser.parse_args()
    result = json.loads(options.result.read_text(encoding="utf-8"))
    profile = json.loads(Path("delivery_template/feature_profile.json").read_text(encoding="utf-8"))
    extended = bool(result.get("case_plan"))
    output = Path("results/dashboard-qualification-acceptance" if extended else "results/dashboard-acceptance")
    output.mkdir(parents=True, exist_ok=True)
    errors = []

    def upload(name, data):
        return {"name": name, "mimeType": "application/json", "buffer": json.dumps(data).encode("utf-8")}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        page = browser.new_page(viewport={"width": 1360, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(Path("delivery_template/dashboard.html").resolve().as_uri())
        page.set_input_files("#files", [upload("result.json", result), upload("feature_profile.json", profile)])
        page.wait_for_function("document.querySelectorAll('#modules .chip').length === 1")
        assert page.locator("#charts .bar").count() == 6
        assert page.locator("#features tr").count() == 9
        assert page.locator("#errors").inner_text() == ""
        page.select_option("#scenario", "S10_loss_5pct_delay_5ms_jitter_2ms" if extended else "weak_network_recovery")
        assert page.locator("#runs tr").count() == result["configuration"]["repeats"] + 1
        if extended:
            assert page.locator("#coverage tr").count() == len(result["case_plan"]) + 1
            assert "S01" in page.locator("#coverage").inner_text() and "S12" in page.locator("#coverage").inner_text()
            page.select_option("#scenario", "S11_publisher_restart")
            assert page.locator("#faults tr").count() == 16
            assert "故障场景" in page.locator("#warnings").inner_text()
        page.screenshot(path=str(output / "vsoa-only.png"), full_page=True)
        fixtures = []
        for slot in range(2, 5):
            fixture = copy.deepcopy(result)
            fixture["module_name"] = f"UI_FIXTURE_{slot}_NOT_A_MIDDLEWARE"
            if slot == 2:
                for run in fixture["runs"]:
                    run["configuration"]["message_size_bytes"] += 1
            fixture_profile = copy.deepcopy(profile)
            fixture_profile["module_name"] = fixture["module_name"]
            fixtures.extend([upload(f"ui-slot-{slot}.json", fixture), upload(f"ui-profile-{slot}.json", fixture_profile)])
        page.set_input_files("#files", fixtures)
        page.wait_for_function("document.querySelectorAll('#modules .chip').length === 4")
        assert page.locator("#charts .bar").count() == 24
        assert "message_size_bytes" in page.locator("#warnings").inner_text()
        assert page.locator("#features tr").first.locator("th").count() == 5
        excess = copy.deepcopy(result)
        excess["module_name"] = "UI_FIXTURE_5_NOT_A_MIDDLEWARE"
        page.set_input_files("#files", upload("too-many.json", excess))
        page.wait_for_function("document.querySelector('#errors').textContent.length > 0")
        assert page.locator("#modules .chip").count() == 4
        page.click("#clear")
        assert page.locator("#modules .chip").count() == 0
        page.set_input_files("#files", {"name": "invalid.json", "mimeType": "application/json", "buffer": b"not json"})
        page.wait_for_function("document.querySelector('#errors').textContent.length > 0")
        assert not errors, errors
        browser.close()
    report = {"passed": True, "extended_coverage_and_fault_timelines": extended, "browser": "local Microsoft Edge, headless",
              "checks": ["real VSOA import", "feature profile rendering", "scenario switching", "four data slots",
                         "configuration mismatch warning", "fifth slot rejected", "clear action", "invalid JSON rejected"],
              "ui_fixture_notice": "Slots 2-5 are in-memory UI fixtures, not other middleware measurements; no fixtures shipped",
              "browser_errors": errors}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
