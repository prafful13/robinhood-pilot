from __future__ import annotations

from unittest.mock import MagicMock

from invoke import Config, Context


def _mock_ctx() -> Context:
    c = Context(config=Config())
    c.run = MagicMock(return_value=MagicMock(ok=True, stdout="", stderr=""))
    return c


class TestK8sLoadImages:
    def test_k8s_load_images_is_noop(self):
        from tasks import k8s_load_images

        mock_context = _mock_ctx()

        k8s_load_images(mock_context)

        mock_context.run.assert_not_called()

    def test_k8s_load_images_prints_explanation(self, capsys):
        from tasks import k8s_load_images

        mock_context = _mock_ctx()

        k8s_load_images(mock_context)

        captured = capsys.readouterr()
        assert "Not needed in Rancher Desktop dockerd mode" in captured.out
        assert "docker build" in captured.out
        assert "images visible to k3s" in captured.out

    def test_k8s_load_images_docstring_references_claude(self):
        from tasks import k8s_load_images

        docstring = k8s_load_images.__doc__
        assert docstring is not None
        assert "CLAUDE.md" in docstring
        assert "Rancher Desktop" in docstring
        assert "dockerd" in docstring
        assert "docker build" in docstring

    def test_k8s_load_images_docstring_mentions_containerd_not_used(self):
        from tasks import k8s_load_images

        docstring = k8s_load_images.__doc__
        assert docstring is not None
        assert "containerd socket" in docstring
        assert "does not exist" in docstring

    def test_k8s_load_images_does_not_run_docker_save_commands(self):
        from tasks import k8s_load_images

        mock_context = _mock_ctx()

        k8s_load_images(mock_context)

        for call in mock_context.run.call_args_list:
            args = call[0]
            if args:
                command = args[0]
                assert "docker save" not in command, "should not run docker save"
                assert "k3s ctr images import" not in command, "should not run k3s ctr"
