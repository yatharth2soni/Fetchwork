"""Shared data models for job postings, cover letters, and session state."""

from __future__ import annotations

from pydantic import BaseModel, Field


class EducationItem(BaseModel):
    institution: str
    degree: str | None = None
    field: str | None = None
    start: str | None = None
    end: str | None = None
    details: list[str] = Field(default_factory=list)


class ExperienceItem(BaseModel):
    company: str
    title: str
    start: str | None = None
    end: str | None = None
    location: str | None = None
    bullets: list[str] = Field(default_factory=list)


class ProjectItem(BaseModel):
    name: str
    description: str | None = None
    url: str | None = None
    technologies: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)


class Resume(BaseModel):
    """Applicant resume. Only facts present in the loaded JSON may be used downstream."""

    name: str
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin: str | None = None
    github: str | None = None
    summary: str | None = None
    education: list[EducationItem] = Field(default_factory=list)
    experience: list[ExperienceItem] = Field(default_factory=list)
    projects: list[ProjectItem] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
