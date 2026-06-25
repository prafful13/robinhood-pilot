from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from invoke import Config, Context


def _mock_ctx() -> Context:
    """Return a real invoke Context with run() mocked out."""
    c = Context(config=Config())
    c.run = MagicMock(return_value=MagicMock(ok=True, stdout="", stderr=""))
    return c


class TestScannerOutputValidation:
    """Test security scanner output validation logic."""

    def test_validate_empty_output_raises(self):
        """Empty scanner output should raise RuntimeError."""
        from tasks import _validate_scanner_output

        with pytest.raises(RuntimeError, match="empty output"):
            _validate_scanner_output("TestScanner", "")

        with pytest.raises(RuntimeError, match="empty output"):
            _validate_scanner_output("TestScanner", "   \n   ")

    def test_validate_output_with_required_markers(self):
        """Output must contain all required markers."""
        from tasks import _validate_scanner_output

        output = '{"results": [], "metrics": {"high": 0}}'
        _validate_scanner_output("TestScanner", output, must_contain=["results", "metrics"])

    def test_validate_output_missing_required_marker(self):
        """Output missing required marker should raise."""
        from tasks import _validate_scanner_output

        output = '{"results": []}'
        with pytest.raises(RuntimeError, match="missing expected marker 'metrics'"):
            _validate_scanner_output("TestScanner", output, must_contain=["results", "metrics"])

    def test_validate_output_no_markers_required(self):
        """Output validation passes when no markers specified."""
        from tasks import _validate_scanner_output

        _validate_scanner_output("TestScanner", "some output here")


class TestBanditScanner:
    """Test Bandit security scanner task."""

    def test_bandit_success_with_valid_json(self):
        mock_c = _mock_ctx()
        """Bandit task succeeds when output contains valid JSON with metrics."""
        from tasks import scan_bandit

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            output_file = tmpdir_path / "bandit.json"

            valid_output = json.dumps(
                {
                    "results": [{"issue_severity": "HIGH", "issue_confidence": "HIGH"}],
                    "metrics": {"_totals": {"HIGH": 1}},
                }
            )
            output_file.write_text(valid_output)

            with patch("tasks.SCAN_DIR", tmpdir_path):
                with patch("tasks._validate_scanner_output"):
                    scan_bandit(mock_c)
                    assert output_file.exists()

    def test_bandit_fails_when_output_file_missing(self):
        mock_c = _mock_ctx()
        """Bandit task fails if output file not created."""
        from tasks import scan_bandit

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            with patch("tasks.SCAN_DIR", tmpdir_path):
                with pytest.raises(RuntimeError, match="failed to create output file"):
                    scan_bandit(mock_c)

    def test_bandit_fails_on_empty_output(self):
        mock_c = _mock_ctx()
        """Bandit task fails if output is empty."""
        from tasks import scan_bandit

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            output_file = tmpdir_path / "bandit.json"
            output_file.write_text("")

            with patch("tasks.SCAN_DIR", tmpdir_path):
                with pytest.raises(RuntimeError, match="empty output"):
                    scan_bandit(mock_c)

    def test_bandit_fails_on_invalid_json(self):
        mock_c = _mock_ctx()
        """Bandit task fails if output is not valid JSON."""
        from tasks import scan_bandit

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            output_file = tmpdir_path / "bandit.json"
            output_file.write_text("invalid json output")

            with patch("tasks.SCAN_DIR", tmpdir_path):
                with patch("tasks._validate_scanner_output"):
                    with pytest.raises(RuntimeError, match="not valid JSON"):
                        scan_bandit(mock_c)


class TestTruffleHogScanner:
    """Test TruffleHog secret detection scanner task."""

    def test_trufflehog_success_with_valid_output(self):
        mock_c = _mock_ctx()
        """TruffleHog task succeeds when output contains expected marker."""
        from tasks import scan_trufflehog

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            output_file = tmpdir_path / "trufflehog.json"
            output_file.write_text("Filesystem scan complete\nNo secrets detected\n")

            with patch("tasks.SCAN_DIR", tmpdir_path):
                with patch("tasks._validate_scanner_output"):
                    scan_trufflehog(mock_c)
                    assert output_file.exists()

    def test_trufflehog_fails_when_output_file_missing(self):
        mock_c = _mock_ctx()
        """TruffleHog task fails if output file not created."""
        from tasks import scan_trufflehog

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            with patch("tasks.SCAN_DIR", tmpdir_path):
                with pytest.raises(RuntimeError, match="failed to create output file"):
                    scan_trufflehog(mock_c)

    def test_trufflehog_fails_on_empty_output(self):
        mock_c = _mock_ctx()
        """TruffleHog task fails if output is empty."""
        from tasks import scan_trufflehog

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            output_file = tmpdir_path / "trufflehog.json"
            output_file.write_text("")

            with patch("tasks.SCAN_DIR", tmpdir_path):
                with pytest.raises(RuntimeError, match="empty output"):
                    scan_trufflehog(mock_c)


class TestCheckovScanner:
    """Test Checkov IaC validation scanner task."""

    def test_checkov_success_with_valid_json(self):
        mock_c = _mock_ctx()
        """Checkov task succeeds when output contains valid JSON with summary."""
        from tasks import scan_checkov

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            output_file = tmpdir_path / "checkov.json"

            valid_output = json.dumps(
                {
                    "check_type": "kubernetes",
                    "results": [],
                    "summary": {"passed": 10, "failed": 0, "skipped": 2},
                }
            )
            output_file.write_text(valid_output)

            with patch("tasks.SCAN_DIR", tmpdir_path):
                with patch("tasks._validate_scanner_output"):
                    scan_checkov(mock_c)
                    assert output_file.exists()

    def test_checkov_fails_when_output_file_missing(self):
        mock_c = _mock_ctx()
        """Checkov task fails if output file not created."""
        from tasks import scan_checkov

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            with patch("tasks.SCAN_DIR", tmpdir_path):
                with pytest.raises(RuntimeError, match="failed to create output file"):
                    scan_checkov(mock_c)

    def test_checkov_fails_on_empty_output(self):
        mock_c = _mock_ctx()
        """Checkov task fails if output is empty."""
        from tasks import scan_checkov

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            output_file = tmpdir_path / "checkov.json"
            output_file.write_text("")

            with patch("tasks.SCAN_DIR", tmpdir_path):
                with pytest.raises(RuntimeError, match="empty output"):
                    scan_checkov(mock_c)

    def test_checkov_fails_on_invalid_json(self):
        mock_c = _mock_ctx()
        """Checkov task fails if output is not valid JSON."""
        from tasks import scan_checkov

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            output_file = tmpdir_path / "checkov.json"
            output_file.write_text("invalid json output")

            with patch("tasks.SCAN_DIR", tmpdir_path):
                with patch("tasks._validate_scanner_output"):
                    with pytest.raises(RuntimeError, match="not valid JSON"):
                        scan_checkov(mock_c)


class TestScanAllTask:
    """Test the combined scan task that runs all scanners."""

    @patch("tasks.scan_checkov")
    @patch("tasks.scan_trufflehog")
    @patch("tasks.scan_bandit")
    def test_scan_all_succeeds_when_all_scanners_pass(
        self, mock_bandit, mock_truffle_hog, mock_checkov
    ):
        """scan task succeeds when all three scanners pass."""
        from tasks import scan

        mock_c = _mock_ctx()

        scan(mock_c)
        mock_bandit.assert_called_once()
        mock_truffle_hog.assert_called_once()
        mock_checkov.assert_called_once()

    @patch("tasks.scan_checkov")
    @patch("tasks.scan_trufflehog")
    @patch("tasks.scan_bandit")
    def test_scan_fails_when_bandit_fails(self, mock_bandit, mock_truffle_hog, mock_checkov):
        """scan task fails if Bandit scanner fails."""
        from tasks import scan

        mock_c = _mock_ctx()
        mock_bandit.side_effect = RuntimeError("Bandit output empty")

        with pytest.raises(RuntimeError, match="1 scanner\\(s\\) did not complete"):
            scan(mock_c)

    @patch("tasks.scan_checkov")
    @patch("tasks.scan_trufflehog")
    @patch("tasks.scan_bandit")
    def test_scan_fails_when_multiple_scanners_fail(
        self, mock_bandit, mock_truffle_hog, mock_checkov
    ):
        """scan task fails if multiple scanners fail."""
        from tasks import scan

        mock_c = _mock_ctx()
        mock_bandit.side_effect = RuntimeError("Bandit output empty")
        mock_truffle_hog.side_effect = RuntimeError("TruffleHog output empty")

        with pytest.raises(RuntimeError, match="2 scanner\\(s\\) did not complete"):
            scan(mock_c)

    @patch("tasks.scan_checkov")
    @patch("tasks.scan_trufflehog")
    @patch("tasks.scan_bandit")
    def test_scan_continues_after_scanner_failure(
        self, mock_bandit, mock_truffle_hog, mock_checkov
    ):
        """scan task continues running remaining scanners even if one fails."""
        from tasks import scan

        mock_c = _mock_ctx()
        mock_bandit.side_effect = RuntimeError("Bandit failed")

        with pytest.raises(RuntimeError):
            scan(mock_c)

        mock_truffle_hog.assert_called_once()
        mock_checkov.assert_called_once()
