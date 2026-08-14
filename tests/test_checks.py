"""The configured-but-unimplemented guards must fire LOUDLY (sharing-axis
design §11: a deferred capability's config key exists with a closed
default, and opening it early is a system-check error, never a no-op)."""
from django.test import override_settings

from stapel_docs.checks import (
    check_doc_types,
    check_download_url_expiry,
    check_exporters,
    check_sharing_axis,
    check_storage_backend,
)


def _ids(errors):
    return sorted(e.id for e in errors)


class TestSharingAxisGuards:
    def test_closed_defaults_are_clean(self):
        assert check_sharing_axis(None) == []

    @override_settings(STAPEL_DOCS={"SHARING": {"MODES": ["whitelist"]}})
    def test_unimplemented_mode_is_an_error(self):
        assert _ids(check_sharing_axis(None)) == ["stapel_docs.E011"]

    @override_settings(STAPEL_DOCS={"SHARING": {"MODES": ["bogus"]}})
    def test_unknown_mode_is_an_error(self):
        assert _ids(check_sharing_axis(None)) == ["stapel_docs.E010"]

    @override_settings(
        STAPEL_DOCS={"SHARING": {"LINK": {"ANONYMOUS": True, "MAX_LEVEL": "edit"}}}
    )
    def test_anonymous_and_edit_link_guards_fire(self):
        assert _ids(check_sharing_axis(None)) == [
            "stapel_docs.E012",
            "stapel_docs.E013",
        ]

    @override_settings(
        STAPEL_DOCS={"SHARING": {"RESOLVERS": {"chat:conversation": "nope.missing"}}}
    )
    def test_broken_resolver_is_an_error(self):
        assert _ids(check_sharing_axis(None)) == ["stapel_docs.E014"]


class TestRegistryGuards:
    def test_defaults_are_clean(self):
        assert check_storage_backend(None) == []
        assert check_doc_types(None) == []
        assert check_exporters(None) == []

    @override_settings(STAPEL_DOCS={"STORAGE": "nope.missing.Backend"})
    def test_broken_storage_path(self):
        assert _ids(check_storage_backend(None)) == ["stapel_docs.E001"]

    @override_settings(STAPEL_DOCS={"DOC_TYPES": {"odt": "nope.missing.spec"}})
    def test_broken_doc_type_overlay(self):
        assert _ids(check_doc_types(None)) == ["stapel_docs.E002"]

    @override_settings(STAPEL_DOCS={"EXPORTERS": {"docx": "nope.missing.Exporter"}})
    def test_broken_exporter_overlay(self):
        assert _ids(check_exporters(None)) == ["stapel_docs.E020"]


class TestDownloadUrlGuards:
    """A download URL that never expires is a read path around authorize().
    The default backend cannot sign, so the operator is told which of the
    two positions the deployment is in — refusing, or handing out permanent
    public links on purpose."""

    def test_default_backend_warns_that_download_urls_are_refused(self):
        assert _ids(check_download_url_expiry(None)) == ["stapel_docs.W031"]

    @override_settings(STAPEL_DOCS={"ALLOW_UNEXPIRING_DOWNLOAD_URLS": True})
    def test_opting_into_permanent_links_is_stated_loudly(self):
        errors = check_download_url_expiry(None)
        assert _ids(errors) == ["stapel_docs.W032"]
        assert "permanent public link" in errors[0].msg

    @override_settings(STAPEL_DOCS={"STORAGE": "stapel_docs.storage.S3Backend"})
    def test_a_signing_backend_is_clean(self):
        assert check_download_url_expiry(None) == []
