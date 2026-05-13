"""Discord UI component builders for marketplace plugins.

These produce JSON dicts matching Discord's component schema.
Used with ``ctx.interaction.respond(components=[...])`` or
``ctx.discord.send_message(channel_id, components=[...])``.

Usage::

    from mmo_maid_sdk import ActionRow, Button, SelectMenu, TextInput

    # Buttons in a row
    row = ActionRow(
        Button("Click me", custom_id="btn_click", style="primary"),
        Button("Cancel", custom_id="btn_cancel", style="secondary"),
    )

    # Select menu
    row2 = ActionRow(
        SelectMenu("pick_role", options=[
            SelectOption("Tank", "tank", emoji="🛡"),
            SelectOption("Healer", "healer", emoji="💚"),
            SelectOption("DPS", "dps", emoji="⚔"),
        ], placeholder="Pick your role"),
    )

    # Modal text inputs (used with ctx.interaction.send_modal)
    fields = [
        TextInput("Character Name", "char_name", placeholder="e.g. Aerilyn"),
        TextInput("Notes", "notes", style="paragraph", required=False),
    ]
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = [
    "ActionRow",
    "Button",
    "SelectMenu",
    "SelectOption",
    "TextInput",
]

# Discord component types
_COMPONENT_ACTION_ROW = 1
_COMPONENT_BUTTON = 2
_COMPONENT_SELECT_MENU = 3
_COMPONENT_TEXT_INPUT = 4

# Button styles
_BUTTON_STYLES = {
    "primary": 1,
    "secondary": 2,
    "success": 3,
    "danger": 4,
    "link": 5,
}

# Text input styles
_TEXT_INPUT_STYLES = {
    "short": 1,
    "paragraph": 2,
}


class Button:
    """A clickable button component.

    Args:
        label: Button text (max 80 chars).
        custom_id: Developer-defined ID for handling clicks (max 100 chars).
            Not needed for link buttons.
        style: One of "primary", "secondary", "success", "danger", "link".
        emoji: Optional emoji string (e.g., "🎮" or a custom emoji dict).
        url: URL for link-style buttons (required if style="link").
        disabled: Whether the button is greyed out.
    """

    def __init__(
        self,
        label: str,
        custom_id: str = "",
        *,
        style: str = "primary",
        emoji: Optional[str] = None,
        url: Optional[str] = None,
        disabled: bool = False,
    ):
        self.label = str(label)[:80]
        self.custom_id = str(custom_id)[:100]
        self.style = style
        self.emoji = emoji
        self.url = url
        self.disabled = disabled

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "type": _COMPONENT_BUTTON,
            "style": _BUTTON_STYLES.get(self.style, 1),
            "label": self.label,
        }
        if self.style == "link" and self.url:
            d["url"] = self.url
        elif self.custom_id:
            d["custom_id"] = self.custom_id
        if self.emoji:
            if isinstance(self.emoji, str):
                d["emoji"] = {"name": self.emoji}
            elif isinstance(self.emoji, dict):
                d["emoji"] = self.emoji
        if self.disabled:
            d["disabled"] = True
        return d


class SelectOption:
    """A single option within a SelectMenu.

    Args:
        label: Display text (max 100 chars).
        value: Developer-defined value returned on select (max 100 chars).
        description: Optional secondary text (max 100 chars).
        emoji: Optional emoji string.
        default: Whether this option is pre-selected.
    """

    def __init__(
        self,
        label: str,
        value: str,
        *,
        description: Optional[str] = None,
        emoji: Optional[str] = None,
        default: bool = False,
    ):
        self.label = str(label)[:100]
        self.value = str(value)[:100]
        self.description = str(description)[:100] if description else None
        self.emoji = emoji
        self.default = default

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"label": self.label, "value": self.value}
        if self.description:
            d["description"] = self.description
        if self.emoji:
            if isinstance(self.emoji, str):
                d["emoji"] = {"name": self.emoji}
            elif isinstance(self.emoji, dict):
                d["emoji"] = self.emoji
        if self.default:
            d["default"] = True
        return d


class SelectMenu:
    """A dropdown select menu component.

    Args:
        custom_id: Developer-defined ID for handling selections (max 100 chars).
        options: List of SelectOption objects (max 25).
        placeholder: Greyed-out text when nothing is selected (max 150 chars).
        min_values: Minimum selections required (default 1).
        max_values: Maximum selections allowed (default 1).
        disabled: Whether the menu is greyed out.
    """

    def __init__(
        self,
        custom_id: str,
        options: Optional[List[SelectOption]] = None,
        *,
        placeholder: str = "",
        min_values: int = 1,
        max_values: int = 1,
        disabled: bool = False,
    ):
        self.custom_id = str(custom_id)[:100]
        self.options = (options or [])[:25]
        self.placeholder = str(placeholder)[:150]
        self.min_values = min_values
        self.max_values = max_values
        self.disabled = disabled

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "type": _COMPONENT_SELECT_MENU,
            "custom_id": self.custom_id,
            "options": [o.to_dict() for o in self.options],
        }
        if self.placeholder:
            d["placeholder"] = self.placeholder
        if self.min_values != 1:
            d["min_values"] = self.min_values
        if self.max_values != 1:
            d["max_values"] = self.max_values
        if self.disabled:
            d["disabled"] = True
        return d


class TextInput:
    """A text input field for modal dialogs.

    Args:
        label: Input label shown to the user (max 45 chars).
        custom_id: Developer-defined ID (max 100 chars).
        style: "short" (single line) or "paragraph" (multi-line).
        placeholder: Greyed-out placeholder text (max 100 chars).
        value: Pre-filled value (max 4000 chars).
        required: Whether the field must be filled.
        min_length: Minimum input length (0-4000).
        max_length: Maximum input length (1-4000).
    """

    def __init__(
        self,
        label: str,
        custom_id: str,
        *,
        style: str = "short",
        placeholder: str = "",
        value: str = "",
        required: bool = True,
        min_length: Optional[int] = None,
        max_length: Optional[int] = None,
    ):
        self.label = str(label)[:45]
        self.custom_id = str(custom_id)[:100]
        self.style = style
        self.placeholder = str(placeholder)[:100]
        self.value = str(value)[:4000] if value else ""
        self.required = required
        self.min_length = min_length
        self.max_length = max_length

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "type": _COMPONENT_TEXT_INPUT,
            "custom_id": self.custom_id,
            "style": _TEXT_INPUT_STYLES.get(self.style, 1),
            "label": self.label,
        }
        if self.placeholder:
            d["placeholder"] = self.placeholder
        if self.value:
            d["value"] = self.value
        d["required"] = self.required
        if self.min_length is not None:
            d["min_length"] = self.min_length
        if self.max_length is not None:
            d["max_length"] = self.max_length
        return d


class ActionRow:
    """A container row for up to 5 components.

    An ActionRow can contain either:
    - Up to 5 Button components, OR
    - 1 SelectMenu component, OR
    - 1 TextInput component (in modals only)

    Args:
        *children: Component objects (Button, SelectMenu, or TextInput).
    """

    def __init__(self, *children):
        self.children = list(children)[:5]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": _COMPONENT_ACTION_ROW,
            "components": [c.to_dict() for c in self.children],
        }
