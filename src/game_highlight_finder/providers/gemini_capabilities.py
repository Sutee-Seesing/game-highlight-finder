"""Model-aware Gemini capability and thinking configuration rules.

The Gemini Interactions API accepts a qualitative ``thinking_level`` for
Gemini 3.x models, while Gemini 2.5 Flash-Lite uses the model's default
thinking-off behavior unless a supported level is explicitly requested.  The
application keeps a single user-facing ``minimal`` setting for the baseline,
but resolves it to the correct wire-level semantics per model before any
provider boundary is crossed.
"""

from __future__ import annotations

from dataclasses import dataclass

MODEL_DEFAULT_MINIMUM_THINKING = "MODEL_DEFAULT_MINIMUM_THINKING"

GEMINI_MODEL_IDS = ("gemini-2.5-flash-lite", "gemini-3.5-flash-lite")


@dataclass(frozen=True)
class GeminiThinkingConfig:
    """Resolved semantic and wire-level thinking configuration."""

    model: str
    configured_level: str
    wire_level: str | None
    effective_mode: str
    reserved_thinking_tokens: int
    policy: str = MODEL_DEFAULT_MINIMUM_THINKING

    @property
    def wire_generation_config(self) -> dict[str, str]:
        """Return only the supported wire-level fields.

        ``None`` deliberately means omission.  Sending ``minimal`` to
        Gemini 2.5 Flash-Lite is not supported by the documented model
        contract, and omitting the field selects its default thinking-off
        behavior.
        """

        if self.wire_level is None:
            return {}
        return {"thinking_level": self.wire_level}

    def payload(self) -> dict[str, object]:
        """Return a safe, deterministic representation for cache/request IDs."""

        return {
            "policy": self.policy,
            "model": self.model,
            "configured_level": self.configured_level,
            "wire_level": self.wire_level,
            "effective_mode": self.effective_mode,
            "reserved_thinking_tokens": self.reserved_thinking_tokens,
        }


def resolve_gemini_thinking_config(
    model: str,
    configured_level: str = "minimal",
    reserved_thinking_tokens: int = 1_024,
) -> GeminiThinkingConfig:
    """Resolve the baseline thinking policy for one supported Gemini model.

    ``minimal`` is retained as the application-level baseline input so old
    config files remain readable.  For both accepted models it means the
    documented model default at the wire boundary: thinking off for 2.5
    Flash-Lite and minimal for 3.5 Flash-Lite.  Explicit ``low``/``medium``/
    ``high`` values are sent unchanged to models that document them.
    """

    if model not in GEMINI_MODEL_IDS:
        raise ValueError(f"Unsupported Gemini model for thinking policy: {model!r}")
    if configured_level not in {"minimal", "low", "medium", "high"}:
        raise ValueError(f"Unsupported Gemini thinking level: {configured_level!r}")
    if reserved_thinking_tokens < 0:
        raise ValueError("Reserved thinking tokens cannot be negative")

    if configured_level == "minimal":
        if model == "gemini-2.5-flash-lite":
            return GeminiThinkingConfig(
                model=model,
                configured_level=configured_level,
                wire_level=None,
                effective_mode="default_off",
                reserved_thinking_tokens=0,
            )
        return GeminiThinkingConfig(
            model=model,
            configured_level=configured_level,
            wire_level=None,
            effective_mode="default_minimal",
            reserved_thinking_tokens=reserved_thinking_tokens,
        )

    return GeminiThinkingConfig(
        model=model,
        configured_level=configured_level,
        wire_level=configured_level,
        effective_mode=configured_level,
        reserved_thinking_tokens=reserved_thinking_tokens,
    )


def validate_wire_thinking_level(model: str, wire_level: str | None) -> None:
    """Fail closed if an adapter attempts to emit an unsupported wire value."""

    if model not in GEMINI_MODEL_IDS:
        raise ValueError(f"Unsupported Gemini model for thinking policy: {model!r}")
    if wire_level is None:
        return
    if wire_level not in {"low", "medium", "high"}:
        raise ValueError(
            f"Unsupported wire thinking level {wire_level!r} for {model}; omit the field "
            "for the model-default minimum policy."
        )
