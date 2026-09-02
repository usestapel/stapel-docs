"""Localized error catalogs (``translations/errors.<lang>.json``) + provenance gate.

i18n-shipping.md §5. This module owns 32 ``error.*.docs_*`` keys and shipped
no catalog for any of them. Since stapel-core 0.23.1 a reader resolves a key it
does not own from the **owner's** catalog
(:func:`stapel_core.i18n.catalogs.module_catalog`), and since 0.22.0 a writer
may only translate the keys it owns — so "stapel-docs ships no catalog" did not
mean "a consumer can fix it in its own tree", it meant every consumer fell back
to the English literal, and the one that filled the gap locally was maintaining
a shadow of somebody else's canon. The keys are this module's; so are their
translations.

Provenance of the localized values (honest, per §5):

* the curated ``stapel-translate`` builtin corpus carries none of these keys —
  they are this module's own vocabulary, not the fleet's cross-cutting HTTP
  errors — so the seed pass fills nothing and every value here is a **machine
  translation** recorded per language in :data:`_MACHINE` and written with
  ``origin: llm`` (unreviewed — the gate's W-counter). In a live deployment
  ``translate_catalogs --domain errors --lang <lang> --llm`` produces these
  through the ``STAPEL_I18N["TRANSLATOR"]`` comm seam; offline they come from
  that map so the module regenerates deterministically without a live LLM;
* the seed pass stays wired anyway: the day one of these keys is promoted into
  the corpus, regenerating picks the curated string up as
  ``origin: seed:stapel-builtin`` with no change here.

Languages match what every other stapel library with error keys promises
(stapel-auth, -billing, -gdpr, -notifications, -profiles, -workspaces): en is
the canon in ``errors.py``, ru and es ship as catalogs. Adding a language is a
three-line change: append the tag to :data:`LANGUAGES`, add its
``_MACHINE_<TAG>`` table, and regenerate.

Regenerate after adding/changing an error key or a translation:

    STAPEL_REGEN_ERROR_I18N=1 python -m pytest tests/test_error_i18n.py::test_regen

then commit ``translations/errors.<lang>.json`` + ``translations/.state.json``.
Without the env var the same module is the CI gate.
"""
import os
from pathlib import Path

from stapel_core.i18n import (
    check_translation_catalogs,
    source_texts,
    summarize,
    translate_catalog,
)
from stapel_core.i18n.catalogs import load_catalog_file

REPO = Path(__file__).resolve().parent.parent
TRANSLATIONS = REPO / "translations"
#: Languages this module ships error catalogs in. en is the canon (the
#: registry literals); every other tag needs a catalog.
LANGUAGES = ["en", "ru", "es"]
#: The languages that need a catalog — everything but the source language.
TARGET_LANGUAGES = [lang for lang in LANGUAGES if lang != "en"]

#: stapel-translate builtin fixtures (the curated seed corpus). Overridable for
#: an out-of-tree checkout via STAPEL_TRANSLATE_FIXTURES.
_FIXTURES = Path(
    os.environ.get(
        "STAPEL_TRANSLATE_FIXTURES",
        REPO.parent / "stapel-translate" / "fixtures" / "builtin",
    )
)

#: Machine translations (origin: llm) of this module's own error keys. All
#: param-free — edit here + regen when the en text changes.
_MACHINE_RU = {
    # Document types and bodies.
    "error.400.docs_unknown_type": "Неизвестный тип документа",
    "error.400.docs_type_not_editable":
        "У этого типа документа нет редактируемого содержимого",
    "error.400.docs_updates_not_crdt":
        "Запись в журнал обновлений допустима только для типов с "
        "CRDT-дисциплиной",
    "error.400.docs_bad_since":
        "Некорректный номер последовательности в параметре ?since=",
    # Tree operations.
    "error.400.docs_folder_depth":
        "Превышена предельная глубина дерева папок",
    "error.400.docs_folder_cycle":
        "Папку нельзя переместить внутрь самой себя",
    "error.400.docs_duplicate_name":
        "Элемент с таким именем здесь уже существует",
    "error.400.docs_not_trashed": "Элемент не находится в корзине",
    "error.400.docs_export_format": "Неизвестный формат экспорта",
    # Upload sessions (DOCS-01/DOCS-03, 0.2.0).
    "error.400.docs_upload_state":
        "Сессия загрузки находится в состоянии, в котором это действие "
        "невозможно",
    "error.400.docs_upload_mismatch":
        "Загруженный объект не соответствует тому, что было заявлено в сессии "
        "загрузки",
    "error.400.docs_upload_mime": "Этот тип содержимого загружать нельзя",
    "error.400.docs_upload_expired": "Срок действия сессии загрузки истёк",
    "error.400.docs_upload_unmeasurable":
        "Не удалось определить размер загруженного объекта",
    "error.400.docs_too_many_updates":
        "Слишком много обновлений в одном запросе",
    "error.400.docs_too_many_uploads":
        "В этом рабочем пространстве уже открыто слишком много сессий загрузки",
    # Authorization.
    "error.403.docs_forbidden": "У вас нет доступа к этому документу",
    "error.403.docs_upload_owner":
        "Завершить загрузку может только пользователь, который её начал",
    # Not found.
    "error.404.docs_document_not_found": "Документ не найден",
    "error.404.docs_folder_not_found": "Папка не найдена",
    "error.404.docs_revision_not_found": "Версия не найдена",
    "error.404.docs_upload_not_found": "Сессия загрузки не найдена",
    # Concurrency.
    "error.409.docs_seq_conflict":
        "Кто-то другой сохранил более новую версию",
    "error.412.docs_missing_if_match":
        "Для сохранения снимка нужен заголовок If-Match с номером "
        "последовательности",
    # Resource ceilings (DOCS-01).
    "error.413.docs_body_too_large":
        "Содержимое документа превышает предельный размер",
    "error.413.docs_update_too_large":
        "Обновление превышает предельный размер",
    "error.413.docs_upload_too_large":
        "Загрузка превышает предельный размер",
    "error.413.docs_export_too_large":
        "Документ слишком велик для экспорта",
    "error.507.docs_workspace_quota":
        "Квота хранилища рабочего пространства исчерпана",
    # Seams that can be absent.
    "error.503.docs_workspaces_unavailable":
        "Сервис участников рабочего пространства недоступен",
    "error.503.docs_exporter_unavailable":
        "Бэкенд экспорта не установлен",
    "error.503.docs_download_url_unavailable":
        "При такой конфигурации хранилища ссылки для скачивания недоступны",
    "error.503.docs_thumbnails_unavailable":
        "Генератор миниатюр не установлен",
    # Previews.
    "error.400.docs_thumbnail_tier": "Неизвестный размер миниатюры",
    "error.400.docs_thumbnail_unsupported":
        "У этого документа нет изображения для предпросмотра",
    # Sharing axis (0.6.0).
    "error.400.docs_share_mode_disabled":
        "Этот способ предоставления доступа отключён в этой инсталляции",
    "error.400.docs_share_level":
        "Такой уровень доступа здесь выдавать нельзя",
    "error.400.docs_share_subject":
        "У правила доступа должен быть ровно один субъект",
    "error.400.docs_share_ref_kind":
        "Для ссылок такого вида не зарегистрирован резолвер",
    "error.401.docs_share_auth_required":
        "Войдите, чтобы открыть документ по ссылке",
    "error.404.docs_access_not_found": "Правило доступа не найдено",
    "error.404.docs_link_not_found": "Ссылка доступа не найдена",
}

_MACHINE_ES = {
    # Document types and bodies.
    "error.400.docs_unknown_type": "Tipo de documento desconocido",
    "error.400.docs_type_not_editable":
        "Este tipo de documento no tiene contenido editable",
    "error.400.docs_updates_not_crdt":
        "El diario de actualizaciones solo admite escrituras para tipos con "
        "disciplina CRDT",
    "error.400.docs_bad_since":
        "El número de secuencia del parámetro ?since= no es válido",
    # Tree operations.
    "error.400.docs_folder_depth":
        "Se ha superado el límite de profundidad del árbol de carpetas",
    "error.400.docs_folder_cycle":
        "Una carpeta no puede moverse dentro de sí misma",
    "error.400.docs_duplicate_name":
        "Ya existe un elemento con este nombre aquí",
    "error.400.docs_not_trashed": "El elemento no está en la papelera",
    "error.400.docs_export_format": "Formato de exportación desconocido",
    # Upload sessions (DOCS-01/DOCS-03, 0.2.0).
    "error.400.docs_upload_state":
        "La sesión de subida no está en un estado que permita esta operación",
    "error.400.docs_upload_mismatch":
        "El objeto subido no coincide con lo que declaró la sesión de subida",
    "error.400.docs_upload_mime": "No se permite subir este tipo de contenido",
    "error.400.docs_upload_expired": "La sesión de subida ha caducado",
    "error.400.docs_upload_unmeasurable":
        "No se ha podido determinar el tamaño del objeto subido",
    "error.400.docs_too_many_updates":
        "Demasiadas actualizaciones en una sola solicitud",
    "error.400.docs_too_many_uploads":
        "Ya hay demasiadas sesiones de subida abiertas en este espacio de "
        "trabajo",
    # Authorization.
    "error.403.docs_forbidden": "No tienes acceso a este documento",
    "error.403.docs_upload_owner":
        "Solo el usuario que abrió esta subida puede finalizarla",
    # Not found.
    "error.404.docs_document_not_found": "Documento no encontrado",
    "error.404.docs_folder_not_found": "Carpeta no encontrada",
    "error.404.docs_revision_not_found": "Revisión no encontrada",
    "error.404.docs_upload_not_found": "Sesión de subida no encontrada",
    # Concurrency.
    "error.409.docs_seq_conflict":
        "Otra persona ha guardado una versión más reciente",
    "error.412.docs_missing_if_match":
        "Para guardar una instantánea se requiere una secuencia If-Match",
    # Resource ceilings (DOCS-01).
    "error.413.docs_body_too_large":
        "El contenido del documento supera el límite de tamaño",
    "error.413.docs_update_too_large":
        "La actualización supera el límite de tamaño",
    "error.413.docs_upload_too_large":
        "La subida supera el límite de tamaño",
    "error.413.docs_export_too_large":
        "El documento es demasiado grande para exportarlo",
    "error.507.docs_workspace_quota":
        "La cuota de almacenamiento del espacio de trabajo está agotada",
    # Seams that can be absent.
    "error.503.docs_workspaces_unavailable":
        "El servicio de miembros del espacio de trabajo no está disponible",
    "error.503.docs_exporter_unavailable":
        "El backend de exportación no está instalado",
    "error.503.docs_download_url_unavailable":
        "Los enlaces de descarga no están disponibles con esta configuración "
        "de almacenamiento",
    "error.503.docs_thumbnails_unavailable":
        "El generador de miniaturas no está instalado",
    # Previews.
    "error.400.docs_thumbnail_tier": "Tamaño de miniatura desconocido",
    "error.400.docs_thumbnail_unsupported":
        "Este documento no tiene vista previa de imagen",
    # Sharing axis (0.6.0).
    "error.400.docs_share_mode_disabled":
        "Esta forma de compartir está desactivada en esta instalación",
    "error.400.docs_share_level":
        "Ese nivel de acceso no puede concederse aquí",
    "error.400.docs_share_subject":
        "Una concesión de acceso nombra exactamente a un sujeto",
    "error.400.docs_share_ref_kind":
        "No hay ningún resolutor registrado para este tipo de referencia",
    "error.401.docs_share_auth_required":
        "Inicia sesión para abrir este documento compartido",
    "error.404.docs_access_not_found": "Concesión de acceso no encontrada",
    "error.404.docs_link_not_found": "Enlace de acceso no encontrado",
}

#: language -> machine-translation table, consulted for the keys the curated
#: corpus does not carry (today: all of them). Values land as ``origin: llm``.
_MACHINE = {"ru": _MACHINE_RU, "es": _MACHINE_ES}


class _DictTranslator:
    """Offline translator seam — returns fixed machine translations by key."""

    def __init__(self, table):
        self._table = table

    def translate(self, entries, source_language, target_language):
        return {k: self._table[k] for k in entries if k in self._table}


def _seed_from_fixtures(lang: str) -> dict[str, str]:
    """Flat ``{error.*: text}`` seed from the builtin fixtures for *lang*."""
    import json

    path = _FIXTURES / f"{lang}.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and k.startswith("error.")
        and isinstance(v, str) and v
    }


def _regen(lang: str):
    """Materialize one target-language catalog from corpus + machine map."""
    return translate_catalog(
        "errors", lang, TRANSLATIONS,
        source_texts=source_texts("errors"),
        seed=_seed_from_fixtures(lang),
        seed_label="stapel-builtin",
        llm=True,
        translator=_DictTranslator(_MACHINE.get(lang, {})),
    )


def test_regen():
    """Regenerate (env-gated) or assert every catalog is a no-op regen (drift)."""
    if os.environ.get("STAPEL_REGEN_ERROR_I18N"):
        for lang in TARGET_LANGUAGES:
            result = _regen(lang)
            assert not result.missing, f"{lang}: still missing: {result.missing}"
        return

    for lang in TARGET_LANGUAGES:
        path = TRANSLATIONS / f"errors.{lang}.json"
        before = path.read_bytes()
        _regen(lang)
        assert path.read_bytes() == before, (
            f"errors.{lang}.json drifted — run "
            f"STAPEL_REGEN_ERROR_I18N=1 pytest tests/test_error_i18n.py::test_regen"
        )


def test_catalog_gate_green():
    """E: missing / stale / params-mismatch / not-byte-stable — all zero."""
    issues = check_translation_catalogs(
        "errors", TRANSLATIONS,
        source_texts=source_texts("errors"),
        languages=LANGUAGES,
    )
    errors, _warnings = summarize(issues)
    blocking = [i for i in issues if i.level == "error"]
    assert not blocking, "\n".join(f"[{i.code}] {i.message}" for i in blocking)
    assert errors == 0


def test_every_language_covers_every_key_this_module_owns():
    """Coverage is scoped to OWNERSHIP: every docs key, in every language."""
    from stapel_core.i18n import owned_keys, owner_of_dir, source_owners

    source = owned_keys(
        source_texts("errors"),
        source_owners("errors"),
        owner_of_dir(TRANSLATIONS),
    )
    assert source, "ownership resolved to nothing — is stapel_docs installed?"
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        missing = [k for k in source if k not in catalog]
        assert not missing, (
            f"{lang} catalog missing {len(missing)} key(s): {missing[:8]}"
        )


def test_this_module_owns_only_its_own_keys():
    """The catalogs carry docs keys and nothing else (no fleet-wide copies).

    The mirror image of the gap these catalogs close: a module that
    translates a key it does not own ships a second, drifting copy of
    somebody else's canon.
    """
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        stray = [k for k in catalog if ".docs_" not in k]
        assert not stray, f"{lang}: not this module's keys: {stray}"


def test_translations_preserve_placeholders():
    """Every localized text keeps exactly the canon's ``{param}`` slots (§3)."""
    from stapel_core.i18n.domains import params_of

    source = source_texts("errors")
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        for key, text in catalog.items():
            if key in source:
                assert set(params_of(text)) == set(params_of(source[key])), \
                    f"{lang}: {key}"
