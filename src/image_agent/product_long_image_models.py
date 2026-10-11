"""Displayed copy and full-frame designs, with optional legacy source notes."""

from typing import Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from .models import Id, Model


class ProductLongImageCopy(Model):
    copy_id: Id = "copy"
    text: str = Field(min_length=1, max_length=500)
    source_kind: str = Field(default="editorial", max_length=80)
    source_quote: str = Field(default="", max_length=12000)
    fact_ids: list[Id] = Field(default_factory=list, max_length=50)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("copy must not be blank")
        return value


class ProductLongImageSection(Model):
    section_id: Id
    role: str = Field(default="custom", min_length=1, max_length=80)
    title: str = Field(default="", max_length=500)
    copy_items: list[ProductLongImageCopy] = Field(default_factory=list, max_length=30)
    visual_prompt: str = Field(min_length=1, max_length=6000)

    @field_validator("visual_prompt")
    @classmethod
    def nonblank_direction(cls, value):
        if not value.strip():
            raise ValueError("visual_prompt must not be blank")
        return value

    @model_validator(mode="before")
    @classmethod
    def legacy_copy_field(cls, value):
        if isinstance(value, dict) and "copy" in value:
            value = dict(value)
            legacy = value.pop("copy")
            value.setdefault("copy_items", legacy)
        return value

    @property
    def copy(self):
        return self.copy_items


def displayed_copy(section):
    """Render titles without requiring duplicate bookkeeping in the body."""
    return list(
        dict.fromkeys([section.title] * bool(section.title) + [c.text for c in section.copy_items])
    )


class ProductLongImagePlan(Model):
    schema_version: Literal["1.0"] = "1.0"
    input_fingerprint: str = Field(default="", max_length=128)
    style_direction: str = Field(min_length=1, max_length=6000)
    sections: list[ProductLongImageSection] = Field(min_length=3, max_length=12)
    # Runtime binds this to the actual selection; provider-authored values are ignored.
    source_evidence: dict = Field(default_factory=dict)

    @field_validator("style_direction")
    @classmethod
    def nonblank_style(cls, value):
        if not value.strip():
            raise ValueError("style_direction must not be blank")
        return value


class ProductLongImageCopyAudit(Model):
    passed: StrictBool
    text_matches: StrictBool
    no_invented_claims: StrictBool
    layout_readable: StrictBool
    issues: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def enforce_checks(self):
        # A provider's optimistic summary cannot override failed subchecks.
        object.__setattr__(
            self,
            "passed",
            self.passed and self.text_matches and self.no_invented_claims and self.layout_readable,
        )
        return self
