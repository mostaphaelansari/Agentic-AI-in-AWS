from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "opensource" / "agents" / "custom"))
import service  # noqa: E402


def test_scan_skills_parses_repo_skills():
    skills = {s["name"]: s for s in service._scan_skills()}
    assert set(skills) == {
        "booking-search",
        "code-interpreter",
        "compare-cities",
        "flight-search",
        "forecast-lookup",
        "map-researcher",
        "travel-planner",
        "web-research",
    }
    assert "weather" in skills["forecast-lookup"]["keywords"]
    assert "current_weather=true" in skills["forecast-lookup"]["body"]
    assert "compare" in skills["compare-cities"]["keywords"]
    assert "nearby" in skills["map-researcher"]["keywords"]
    assert "who won" in skills["web-research"]["keywords"]
    assert "calculate" in skills["code-interpreter"]["keywords"]
    assert "availability" in skills["booking-search"]["keywords"]
    assert "flight" in skills["flight-search"]["keywords"]
    assert "itinerary" in skills["travel-planner"]["keywords"]
    # Body must not carry the frontmatter along.
    assert not skills["compare-cities"]["body"].startswith("---")


def test_match_skills_by_keywords():
    names = lambda prompt: sorted(s["name"] for s in service._match_skills(prompt))  # noqa: E731
    assert names("What is the weather in Antibes?") == ["forecast-lookup"]
    assert names("Compare today's weather in Casablanca and Paris.") == [
        "compare-cities",
        "forecast-lookup",
    ]
    assert names("Quel temps fera-t-il demain?") == ["forecast-lookup"]
    assert names("Explain the ReAct pattern.") == []
    assert names("Find museums nearby in Lisbon.") == ["map-researcher"]
    assert names("Build a four-day itinerary for Lisbon.") == ["travel-planner"]
    assert names("Who won the World Cup 2026?") == ["web-research"]
    assert names("Calculate the mean and standard deviation of these values.") == [
        "code-interpreter"
    ]
    assert names("Calcule la moyenne et le pourcentage.") == ["code-interpreter"]
    assert names("Check accommodation availability in Lisbon.") == ["booking-search"]
    assert names("What is the status of flight BA123 on 2026-08-01?") == ["flight-search"]
    assert names("Find hotels in Lisbon with prices in EUR.") == [
        "booking-search",
        "map-researcher",
    ]
    assert names("Plan a weekend trip to Cannes with attractions.") == [
        "forecast-lookup",
        "map-researcher",
        "travel-planner",
    ]


def test_flight_skill_requires_specialty_tools_before_bounded_fallback():
    skills = {skill["name"]: skill for skill in service._scan_skills()}
    body = skills["flight-search"]["body"]
    compact = " ".join(body.split())

    assert "both HND and NRT" in compact
    assert "at most one corrected" in compact
    assert "Only after that retry fails, use `webSearch`" in compact
    assert "browser automation only" in compact


def test_system_prompt_composition(monkeypatch):
    monkeypatch.setattr(service, "_current_date", lambda: "2026-07-20")
    plain = service._system_prompt_for("Explain the ReAct pattern.")
    assert plain.startswith(service.SYSTEM_PROMPT)
    assert plain.endswith("Current date: 2026-07-20.")

    composed = service._system_prompt_for("Will it rain in Lisbon this evening?")
    assert composed.startswith(service.SYSTEM_PROMPT)
    assert "## Skill: forecast-lookup" in composed
    assert "current_weather=true" in composed
    assert "## Skill: compare-cities" not in composed

    calculation = service._system_prompt_for(
        "Calculate the mean and standard deviation of [12, 17, 19, 23]."
    )
    assert "## Skill: code-interpreter" in calculation
    assert "Never ask the user to request `code_interpreter` by name" in calculation

    trip = service._system_prompt_for("Build a four-day itinerary for Lisbon.")
    assert "## Skill: travel-planner" in trip
    assert "day-by-day itinerary" in trip

    # Core rules stay in the instructions half, never in a skill.
    assert "LANGUAGE RULE" in service.SYSTEM_PROMPT
    assert "CONFIDENTIALITY RULE" in service.SYSTEM_PROMPT


def test_missing_skills_dir_is_stateless(monkeypatch):
    monkeypatch.setattr(service, "SKILLS_DIR", "/nonexistent")
    assert service._scan_skills() == []
    assert service._system_prompt_for("weather in Paris").startswith(service.SYSTEM_PROMPT)
