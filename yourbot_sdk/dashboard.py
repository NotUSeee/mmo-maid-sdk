"""Declarative dashboard builder for marketplace plugins.

The platform renders plugin dashboards from a JSON manifest. This module is a
type-hinted Python DSL that compiles to that exact JSON — saves you from
hand-writing JSON, gives IDE autocomplete on widget options, and catches a
class of typos at build time instead of "no error, blank widget at runtime."

Quick example::

    from yourbot_sdk import dashboard

    stats = dashboard.Page("Stats", id="stats")
    stats.add(dashboard.StatCard("Active members", rpc="get_active_count"))
    stats.add(dashboard.Chart.line("Activity (30d)", rpc="get_activity"))

    settings = dashboard.Page("Settings", id="settings")
    form = dashboard.Form(rpc="get_settings", save_rpc="save_settings")
    form.text("Welcome message", key="msg", required=True)
    form.channel_picker("Welcome channel", key="channel_id")
    form.toggle("Enable greeting", key="enabled")
    settings.add(form)

    manifest = dashboard.Manifest().add_page(stats).add_page(settings)
    manifest_json = manifest.to_json()

The output of ``to_json()`` is the same JSON the platform reads from
``dashboard_manifest.json`` in the plugin zip. You can either write it there
at build time or persist via the dev portal API.

This DSL **does not change the platform manifest schema** — it's a Python
builder that compiles to the existing JSON contract. Adding a new field via
the DSL without adding it to the platform's renderer will silently no-op.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


__all__ = [
    "Manifest", "Page",
    "StatCard", "Chart", "Table", "Text", "Alert",
    "ProgressBar", "List", "Markdown", "Form",
    "Column", "Option",
]


# ── Shared helpers ──────────────────────────────────────────────────────────


_WIDGET_WIDTHS = {"full", "half", "third", "two_thirds"}


def _validate_width(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip()
    if v not in _WIDGET_WIDTHS:
        raise ValueError(
            f"width must be one of {sorted(_WIDGET_WIDTHS)!r}, got {value!r}"
        )
    return v


@dataclass
class Column:
    """Table column definition."""
    key: str
    label: Optional[str] = None

    def to_json(self) -> dict:
        return {"key": self.key, "label": self.label or self.key}


@dataclass
class Option:
    """Select / multi_select option."""
    value: str
    label: Optional[str] = None

    def to_json(self) -> dict:
        return {"value": self.value, "label": self.label or self.value}


# ── Widgets ─────────────────────────────────────────────────────────────────


_PERMISSION_VALUES = {"viewer", "manager", "owner"}


def _validate_permission(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip().lower()
    if v not in _PERMISSION_VALUES:
        raise ValueError(
            f"permission must be one of {sorted(_PERMISSION_VALUES)!r}, got {value!r}"
        )
    return v


@dataclass
class _Widget:
    """Internal base — every widget compiles to a dict with at least id+type."""
    id: str
    type: str
    title: Optional[str] = None
    width: Optional[str] = None
    refresh_seconds: Optional[int] = None
    rpc_method: Optional[str] = None
    permission: Optional[str] = None

    def to_json(self) -> dict:
        out: dict[str, Any] = {"id": self.id, "type": self.type}
        if self.title is not None: out["title"] = self.title
        if self.width is not None: out["width"] = _validate_width(self.width)
        if self.refresh_seconds is not None: out["refresh_seconds"] = int(self.refresh_seconds)
        if self.rpc_method is not None: out["rpc_method"] = self.rpc_method
        if self.permission is not None: out["permission"] = _validate_permission(self.permission)
        return out


def _gen_id(prefix: str, counter: list[int]) -> str:
    """Auto-id for widgets when caller doesn't supply one."""
    counter[0] += 1
    return f"{prefix}_{counter[0]}"


@dataclass
class StatCard(_Widget):
    """Single number with optional label/icon/change indicator.

    Handler returns: ``{"value": 1234, "change": "+12%"}``
    """
    label: Optional[str] = None
    icon: Optional[str] = None

    def __init__(
        self, label: str, *, rpc: str, id: Optional[str] = None,
        icon: Optional[str] = None, width: str = "third",
        refresh_seconds: Optional[int] = None,
        permission: Optional[str] = None,
    ):
        _validate_width(width)
        _validate_permission(permission)
        super().__init__(
            id=id or f"stat_{label.lower().replace(' ', '_')[:24]}",
            type="stat_card", title=label, width=width,
            refresh_seconds=refresh_seconds, rpc_method=rpc,
            permission=permission,
        )
        self.label = label
        self.icon = icon

    def to_json(self) -> dict:
        out = super().to_json()
        out["label"] = self.label
        if self.icon is not None: out["icon"] = self.icon
        return out


class Chart(_Widget):
    """Chart widget. Use the factories ``Chart.line(...)``, ``Chart.bar(...)``,
    ``Chart.pie(...)``, ``Chart.gauge(...)`` — they fill in ``chart_type`` correctly.

    Handler returns: ``{"labels": [...], "series": [{"name": "...", "data": [...]}]}``
    """
    _VALID_TYPES = {"line", "bar", "area", "scatter", "pie", "doughnut", "gauge"}

    def __init__(
        self, title: str, *, rpc: str, chart_type: str,
        id: Optional[str] = None, width: str = "half",
        refresh_seconds: Optional[int] = None,
        chart_options: Optional[dict] = None,
        permission: Optional[str] = None,
    ):
        ct = str(chart_type).strip().lower()
        if ct not in self._VALID_TYPES:
            raise ValueError(
                f"chart_type must be one of {sorted(self._VALID_TYPES)!r}, got {chart_type!r}"
            )
        _validate_width(width)
        _validate_permission(permission)
        super().__init__(
            id=id or f"chart_{title.lower().replace(' ', '_')[:24]}",
            type="chart", title=title, width=width,
            refresh_seconds=refresh_seconds, rpc_method=rpc,
            permission=permission,
        )
        self.chart_type = ct
        self.chart_options = chart_options or {}

    def to_json(self) -> dict:
        out = super().to_json()
        out["chart_type"] = self.chart_type
        if self.chart_options:
            out["chart_options"] = dict(self.chart_options)
        return out

    @classmethod
    def line(cls, title: str, *, rpc: str, **kwargs) -> "Chart":
        return cls(title, rpc=rpc, chart_type="line", **kwargs)

    @classmethod
    def bar(cls, title: str, *, rpc: str, **kwargs) -> "Chart":
        return cls(title, rpc=rpc, chart_type="bar", **kwargs)

    @classmethod
    def pie(cls, title: str, *, rpc: str, **kwargs) -> "Chart":
        return cls(title, rpc=rpc, chart_type="pie", **kwargs)

    @classmethod
    def gauge(cls, title: str, *, rpc: str, **kwargs) -> "Chart":
        return cls(title, rpc=rpc, chart_type="gauge", **kwargs)


@dataclass
class Table(_Widget):
    """Tabular widget. Handler returns ``{"rows": [{...}], "total": int}``."""
    columns: list[Column] = field(default_factory=list)

    def __init__(
        self, title: str, *, rpc: str,
        columns: Iterable, id: Optional[str] = None,
        width: str = "full", refresh_seconds: Optional[int] = None,
        permission: Optional[str] = None,
    ):
        _validate_width(width)
        _validate_permission(permission)
        super().__init__(
            id=id or f"table_{title.lower().replace(' ', '_')[:24]}",
            type="table", title=title, width=width,
            refresh_seconds=refresh_seconds, rpc_method=rpc,
            permission=permission,
        )
        self.columns = [
            c if isinstance(c, Column) else (Column(*c) if isinstance(c, (tuple, list)) else Column(str(c)))
            for c in columns
        ]

    def to_json(self) -> dict:
        out = super().to_json()
        out["columns"] = [c.to_json() for c in self.columns]
        return out


class Text(_Widget):
    """Plain text widget. Handler returns ``{"text": "..."}``."""
    def __init__(self, title: str, *, rpc: str, id: Optional[str] = None,
                 width: str = "full", refresh_seconds: Optional[int] = None,
                 permission: Optional[str] = None):
        _validate_width(width); _validate_permission(permission)
        super().__init__(
            id=id or f"text_{title.lower().replace(' ', '_')[:24]}",
            type="text", title=title, width=width,
            refresh_seconds=refresh_seconds, rpc_method=rpc,
            permission=permission,
        )


class Alert(_Widget):
    """Alert box. Handler returns ``{"level": "info"|"warning"|"error"|"success", "message": "..."}``."""
    def __init__(self, title: str, *, rpc: str, id: Optional[str] = None,
                 width: str = "full", refresh_seconds: Optional[int] = None,
                 permission: Optional[str] = None):
        _validate_width(width); _validate_permission(permission)
        super().__init__(
            id=id or f"alert_{title.lower().replace(' ', '_')[:24]}",
            type="alert", title=title, width=width,
            refresh_seconds=refresh_seconds, rpc_method=rpc,
            permission=permission,
        )


class ProgressBar(_Widget):
    """Labeled progress bar. Handler returns ``{"value": 75, "max": 100, "label": "..."}``."""
    def __init__(self, label: str, *, rpc: str, id: Optional[str] = None,
                 width: str = "half", refresh_seconds: Optional[int] = None,
                 permission: Optional[str] = None):
        _validate_width(width); _validate_permission(permission)
        super().__init__(
            id=id or f"progress_{label.lower().replace(' ', '_')[:24]}",
            type="progress_bar", title=label, width=width,
            refresh_seconds=refresh_seconds, rpc_method=rpc,
            permission=permission,
        )
        self.label = label

    def to_json(self) -> dict:
        out = super().to_json()
        out["label"] = self.label
        return out


class List(_Widget):
    """Key-value list. Handler returns ``{"items": [{"label": "...", "value": "..."}, ...]}``."""
    def __init__(self, title: str, *, rpc: str, id: Optional[str] = None,
                 width: str = "half", refresh_seconds: Optional[int] = None,
                 permission: Optional[str] = None):
        _validate_width(width); _validate_permission(permission)
        super().__init__(
            id=id or f"list_{title.lower().replace(' ', '_')[:24]}",
            type="list", title=title, width=width,
            refresh_seconds=refresh_seconds, rpc_method=rpc,
            permission=permission,
        )


class Markdown(_Widget):
    """Markdown content. Handler returns ``{"markdown": "..."}``."""
    def __init__(self, title: str, *, rpc: str, id: Optional[str] = None,
                 width: str = "full", refresh_seconds: Optional[int] = None,
                 permission: Optional[str] = None):
        _validate_width(width); _validate_permission(permission)
        super().__init__(
            id=id or f"md_{title.lower().replace(' ', '_')[:24]}",
            type="markdown", title=title, width=width,
            refresh_seconds=refresh_seconds, rpc_method=rpc,
            permission=permission,
        )


# ── Form widget + fluent builders ───────────────────────────────────────────


@dataclass
class _Field:
    """Single form field. Use Form.<type>(...) builders to construct."""
    key: str
    label: str
    type: str
    required: bool = False
    placeholder: Optional[str] = None
    help_text: Optional[str] = None
    options: Optional[list[Option]] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None

    def to_json(self) -> dict:
        out: dict[str, Any] = {"key": self.key, "label": self.label, "type": self.type}
        if self.required: out["required"] = True
        if self.placeholder is not None: out["placeholder"] = self.placeholder
        if self.help_text is not None: out["help_text"] = self.help_text
        if self.options is not None:
            out["options"] = [o.to_json() for o in self.options]
        if self.min is not None: out["min"] = self.min
        if self.max is not None: out["max"] = self.max
        if self.step is not None: out["step"] = self.step
        return out


class Form(_Widget):
    """A form widget with one or more fields.

    Handler ``rpc_method`` returns the current field values to populate the form.
    ``save_rpc_method`` receives ``{field_key: value}`` on submit and returns
    ``{"ok": True}`` or ``{"error": "..."}``.

    Build fields using fluent methods::

        form = dashboard.Form(rpc="get_settings", save_rpc="save_settings", title="Configure")
        form.text("Welcome message", key="msg", required=True)
        form.number("Cooldown (s)", key="cooldown", min=0, max=3600)
        form.toggle("Enabled", key="enabled")
    """
    def __init__(
        self, *, rpc: str, save_rpc: str, title: Optional[str] = None,
        id: Optional[str] = None, width: str = "full",
        permission: Optional[str] = None,
    ):
        _validate_width(width); _validate_permission(permission)
        super().__init__(
            id=id or "form",
            type="form", title=title, width=width, rpc_method=rpc,
            permission=permission,
        )
        self.save_rpc_method = save_rpc
        self.fields: list[_Field] = []

    def to_json(self) -> dict:
        out = super().to_json()
        out["save_rpc_method"] = self.save_rpc_method
        out["fields"] = [f.to_json() for f in self.fields]
        return out

    # Fluent field builders — each returns self so calls can chain.

    def text(self, label: str, *, key: str, required: bool = False,
             placeholder: Optional[str] = None, help_text: Optional[str] = None) -> "Form":
        self.fields.append(_Field(
            key=key, label=label, type="text",
            required=required, placeholder=placeholder, help_text=help_text,
        ))
        return self

    def textarea(self, label: str, *, key: str, required: bool = False,
                 placeholder: Optional[str] = None, help_text: Optional[str] = None) -> "Form":
        self.fields.append(_Field(
            key=key, label=label, type="textarea",
            required=required, placeholder=placeholder, help_text=help_text,
        ))
        return self

    def number(self, label: str, *, key: str, required: bool = False,
               min: Optional[float] = None, max: Optional[float] = None,
               step: Optional[float] = None, placeholder: Optional[str] = None,
               help_text: Optional[str] = None) -> "Form":
        self.fields.append(_Field(
            key=key, label=label, type="number",
            required=required, placeholder=placeholder, help_text=help_text,
            min=min, max=max, step=step,
        ))
        return self

    def toggle(self, label: str, *, key: str, help_text: Optional[str] = None) -> "Form":
        self.fields.append(_Field(
            key=key, label=label, type="toggle", help_text=help_text,
        ))
        return self

    def color(self, label: str, *, key: str, help_text: Optional[str] = None) -> "Form":
        self.fields.append(_Field(
            key=key, label=label, type="color", help_text=help_text,
        ))
        return self

    def date(self, label: str, *, key: str, required: bool = False,
             help_text: Optional[str] = None) -> "Form":
        self.fields.append(_Field(
            key=key, label=label, type="date",
            required=required, help_text=help_text,
        ))
        return self

    def range(self, label: str, *, key: str, min: float = 0, max: float = 100,
              step: float = 1, help_text: Optional[str] = None) -> "Form":
        self.fields.append(_Field(
            key=key, label=label, type="range",
            min=min, max=max, step=step, help_text=help_text,
        ))
        return self

    def select(self, label: str, *, key: str, options: Iterable,
               required: bool = False, help_text: Optional[str] = None) -> "Form":
        opts = [
            o if isinstance(o, Option) else (Option(*o) if isinstance(o, (tuple, list)) else Option(str(o)))
            for o in options
        ]
        self.fields.append(_Field(
            key=key, label=label, type="select",
            options=opts, required=required, help_text=help_text,
        ))
        return self

    def multi_select(self, label: str, *, key: str, options: Iterable,
                     required: bool = False, help_text: Optional[str] = None) -> "Form":
        opts = [
            o if isinstance(o, Option) else (Option(*o) if isinstance(o, (tuple, list)) else Option(str(o)))
            for o in options
        ]
        self.fields.append(_Field(
            key=key, label=label, type="multi_select",
            options=opts, required=required, help_text=help_text,
        ))
        return self

    def channel_picker(self, label: str, *, key: str, required: bool = False,
                       help_text: Optional[str] = None) -> "Form":
        """Convenience: a 'channel' field type the renderer treats specially.

        Note: the platform renderer maps ``type='channel'`` to a select populated
        with the server's channels. If your platform version doesn't yet support
        that, this still renders as a text field with the channel ID.
        """
        self.fields.append(_Field(
            key=key, label=label, type="channel",
            required=required, help_text=help_text,
        ))
        return self

    def role_picker(self, label: str, *, key: str, required: bool = False,
                    help_text: Optional[str] = None) -> "Form":
        """Convenience: a 'role' field type for role selection."""
        self.fields.append(_Field(
            key=key, label=label, type="role",
            required=required, help_text=help_text,
        ))
        return self


# ── Page + Manifest ─────────────────────────────────────────────────────────


class Page:
    """One dashboard page. Add widgets via ``page.add(widget)`` or pass them in ctor.

    ``permission`` (optional): minimum role required to see this page at all.
    Valid values: ``"viewer"``, ``"manager"``, ``"owner"``. Manifest values
    can only INCREASE the required role above the operation's natural minimum,
    never lower it.
    """
    def __init__(self, title: str, *, id: Optional[str] = None,
                 widgets: Optional[Iterable] = None,
                 permission: Optional[str] = None):
        if not title or not str(title).strip():
            raise ValueError("Page title is required")
        _validate_permission(permission)
        self.title = str(title)
        self.id = id or self.title.lower().replace(" ", "_")[:48]
        self.widgets: list = list(widgets) if widgets else []
        self.permission = permission

    def add(self, widget) -> "Page":
        """Add a widget to this page. Returns self for chaining."""
        if not hasattr(widget, "to_json"):
            raise TypeError(
                f"Page.add() expected a widget (StatCard, Chart, Table, Form, …), got {type(widget).__name__}"
            )
        self.widgets.append(widget)
        return self

    def to_json(self) -> dict:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "widgets": [w.to_json() for w in self.widgets],
        }
        if self.permission is not None:
            out["permission"] = self.permission
        return out


class Manifest:
    """Top-level dashboard manifest. Compose pages via ``manifest.add_page(page)``.

    Final shape::

        {"pages": [Page.to_json(), ...]}

    Optional ``mode`` for iframe dashboards (advanced)::

        Manifest(mode="iframe")
    """
    def __init__(self, *, mode: Optional[str] = None,
                 pages: Optional[Iterable[Page]] = None):
        if mode is not None and mode not in ("manifest", "iframe"):
            raise ValueError(f"mode must be 'manifest' or 'iframe', got {mode!r}")
        self.mode = mode
        self.pages: list[Page] = list(pages) if pages else []

    def add_page(self, page: Page) -> "Manifest":
        if not isinstance(page, Page):
            raise TypeError(f"add_page expected a Page, got {type(page).__name__}")
        self.pages.append(page)
        return self

    def to_json(self) -> dict:
        out: dict[str, Any] = {"pages": [p.to_json() for p in self.pages]}
        if self.mode is not None:
            out["mode"] = self.mode
        return out
