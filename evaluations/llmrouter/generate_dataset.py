"""Generate a reproducible cold-start corpus for Haiku/Sonnet routing.

The generated ``performance`` values are policy utilities, not measured model
quality. They make the upstream KNN router consume the desired cold-start
winner while retaining provenance needed to replace them with evaluation data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

SEED = 20260813
SPLIT_SIZES = {"train": 80, "validation": 10, "test": 10}
MODEL_NAMES = ("cheap", "strong")
POLICY_VERSION = "value-maxxing-bootstrap-v1"
DEFAULT_OUTPUT = Path(__file__).with_name("data")


@dataclass(frozen=True)
class Scenario:
    domain: str
    family: str
    cheap: tuple[str, str, str]
    strong: tuple[str, str, str]
    cheap_rationale: str
    strong_rationale: str
    strong_risk: str = "medium"


TRAVEL_SCENARIOS = (
    Scenario(
        "travel",
        "weather",
        (
            "Check the current weather in {destination}.",
            "What is the forecast for {destination} on {date}?",
            "Tell me whether rain is expected in {destination} {date}.",
        ),
        (
            "Compare weather in {destination} and {alt_destination} across {duration}, then optimize a day-by-day outdoor plan for {travelers}.",
            "Reconcile the forecasts for {destination} and {alt_destination}, explain uncertainty, and recommend which destination better fits {activity} on {date}.",
            "Build a weather-resilient {duration} plan for {destination} with indoor alternatives, packing decisions, and explicit assumptions.",
        ),
        "A single bounded weather lookup is adequately handled by the economical model.",
        "Multi-location synthesis and contingency planning raise the quality requirement.",
    ),
    Scenario(
        "travel",
        "nearby_places",
        (
            "Find three {place_type} near {landmark}.",
            "List a few {place_type} around {landmark} in {destination}.",
            "What {place_type} are close to {landmark}?",
        ),
        (
            "Research {place_type} near {landmark}, verify current visitor details, compare walking routes, and choose the best option for {travelers} with {accessibility}.",
            "Design a two-hour route around {landmark} combining {place_type} and {activity}; reconcile map and official-source information.",
            "Evaluate the safest and most practical {place_type} near {landmark} for {travelers}, including accessibility and current-hours uncertainty.",
        ),
        "Nearby-place discovery with a short list is a bounded retrieval task.",
        "Route optimization and source reconciliation require stronger synthesis.",
    ),
    Scenario(
        "travel",
        "flight_status",
        (
            "Check the status of flight {flight} on {date}.",
            "What are the local departure and arrival times for {flight} {date}?",
            "Is {flight} delayed? Report only what the live data says.",
        ),
        (
            "Flight {flight} may be disrupted on {date}; assess connection risk, compare recovery options, and clearly separate live facts from assumptions.",
            "Analyze whether {travelers} can make a {connection_minutes}-minute connection after {flight}, accounting for terminal changes and uncertain operational data.",
            "Create a disruption decision tree for {flight} with rebooking alternatives, time-zone handling, and escalation points.",
        ),
        "Reporting one flight's returned status is a narrow tool-grounded task.",
        "Disruption and connection decisions combine uncertain, consequential constraints.",
        "high",
    ),
    Scenario(
        "travel",
        "flight_search",
        (
            "Find flight options from {origin} to {destination} on {date}; do not book anything.",
            "Show three flight-search leads between {origin} and {destination} for {date}.",
            "Search for a one-way flight from {origin} to {destination} on {date}.",
        ),
        (
            "Compare multi-city flight strategies from {origin} through {alt_destination} to {destination} for {travelers}, balancing {budget}, duration, baggage uncertainty, and missed-connection risk.",
            "Find and critically compare flight options from {origin} to {destination} across {date}, including tradeoffs among price, stops, local times, and refund uncertainty.",
            "Plan robust flight alternatives for {travelers} from {origin} to {destination} under {budget}, with explicit fallback logic if the preferred itinerary disappears.",
        ),
        "A single search with a few leads is bounded and reversible.",
        "Multi-leg optimization has interacting costs, times, and operational risks.",
    ),
    Scenario(
        "travel",
        "hotels",
        (
            "Find hotel leads near {landmark} under {budget} per night; do not reserve.",
            "List three stays in {destination} for {travelers} on {date}.",
            "Search for accommodation around {neighborhood} in {destination}.",
        ),
        (
            "Compare hotels across {neighborhood} and {alt_neighborhood} for {travelers}, weighing total price, cancellation uncertainty, transit, {accessibility}, and neighborhood fit.",
            "Research where to stay in {destination} under {budget}, verify current claims, and defend a recommendation for a {duration} trip focused on {activity}.",
            "Build a hotel shortlist for {travelers} in {destination}, detect hidden tradeoffs in taxes and policies, and provide a risk-aware recommendation without claiming a reservation.",
        ),
        "Returning accommodation leads is a standard retrieval task.",
        "A defensible stay decision requires multi-criteria comparison and caveats.",
    ),
    Scenario(
        "travel",
        "itinerary",
        (
            "Draft a simple one-day sightseeing outline for {destination}.",
            "Give me a short morning-and-afternoon plan around {landmark}.",
            "List a basic day trip in {destination} focused on {activity}.",
        ),
        (
            "Plan a detailed {duration} trip to {destination} for {travelers} under {budget}, coordinating flights, hotels, weather, transit, {activity}, and realistic recovery time.",
            "Design and justify a day-by-day {duration} itinerary for {destination} that resolves competing interests, {accessibility}, live-data uncertainty, and travel time.",
            "Create an in-depth itinerary for {destination} and explain why each day is sequenced that way, with alternatives for closures, bad weather, and fatigue.",
        ),
        "A low-constraint day outline is acceptable from the economical model.",
        "A multi-day itinerary is a long-horizon orchestration and reasoning task.",
    ),
    Scenario(
        "travel",
        "budget_math",
        (
            "Split a {budget} travel budget equally across {days} days.",
            "Add these trip costs: {cost_a}, {cost_b}, and {cost_c} euros.",
            "Convert a total of {total_cost} euros for {travelers} into a per-person amount.",
        ),
        (
            "Optimize a {budget} budget for {travelers} over {duration} in {destination}, model uncertain transport and lodging prices, and explain the opportunity costs.",
            "Analyze the uploaded travel-cost assumptions, identify outliers and missing categories, then produce a justified budget with sensitivity ranges.",
            "Compare three budget allocations for {destination} and recommend one after stress-testing exchange-rate, hotel, and flight-price changes.",
        ),
        "Simple deterministic arithmetic does not require the strongest model.",
        "Budget optimization and sensitivity analysis require deeper reasoning.",
    ),
    Scenario(
        "travel",
        "packing",
        (
            "Give me a short packing list for {destination} on {date}.",
            "What should I pack for {activity} in {destination}?",
            "List basic clothing for a {duration} visit to {destination}.",
        ),
        (
            "Create a risk-aware packing strategy for {duration} across {destination} and {alt_destination}, accounting for forecast uncertainty, {activity}, baggage limits, and {accessibility}.",
            "Explain in depth how climate, local norms, and planned activities should change the packing list for {travelers} visiting {destination} on {date}.",
            "Reconcile conflicting weather signals and design a minimal packing list with contingency items and a clear decision rationale.",
        ),
        "A generic packing checklist is low-risk and bounded.",
        "Cross-climate optimization and uncertainty management need stronger reasoning.",
    ),
    Scenario(
        "travel",
        "routing_maps",
        (
            "Show a walking route from {landmark} to {second_landmark}.",
            "How far is {landmark} from {second_landmark} in {destination}?",
            "Find {landmark} on the map and list nearby transit.",
        ),
        (
            "Compare walking, public-transit, and accessible routes between {landmark} and {second_landmark}, then recommend a plan under uncertain live conditions.",
            "Design an efficient route through {landmark}, {second_landmark}, and nearby {place_type}, respecting {accessibility} and a two-hour limit.",
            "Assess route safety and practicality for {travelers} across multiple stops in {destination}; explain assumptions and fallback options.",
        ),
        "One route lookup or distance report is a simple tool task.",
        "Multi-stop accessible routing adds consequential constraints.",
    ),
    Scenario(
        "travel",
        "travel_data",
        (
            "Sort these destination scores and return the top three: {score_list}.",
            "Calculate the average daily cost from {cost_list}.",
            "Summarize the highest and lowest values in this small travel table: {score_list}.",
        ),
        (
            "Analyze the travel dataset for bias, missing values, outliers, and misleading correlations, then defend the most decision-useful visualization.",
            "Build an in-depth comparison from the uploaded CSV, validate calculations, segment traveler types, and explain uncertainty in the recommendation.",
            "Audit the methodology behind these destination rankings and determine whether the claimed winner is robust to weighting changes.",
        ),
        "Small transparent calculations and sorting are economical-model tasks.",
        "Data auditing, robustness, and methodological critique require Sonnet.",
    ),
    Scenario(
        "travel",
        "entry_requirements",
        (
            "Point me to the official page for entry requirements for {destination}.",
            "Find the official visa-information source for travel to {destination}.",
            "Which official authority publishes entry rules for {destination}?",
        ),
        (
            "Assess current entry and transit requirements for a complex trip from {origin} through {alt_destination} to {destination}; cite official sources and flag legal uncertainty.",
            "Explain how passport validity, transit rules, and changing entry requirements could affect {travelers} traveling to {destination} on {date}.",
            "Reconcile conflicting official guidance about entry to {destination} and state what must be confirmed with the competent authority.",
        ),
        "Locating an official source is bounded; the answer must avoid interpretation.",
        "Interpreting current entry rules is high-stakes and temporally sensitive.",
        "high",
    ),
    Scenario(
        "travel",
        "accessibility",
        (
            "Find wheelchair-accessibility information for {landmark} from an official source.",
            "List accessible-transport contacts for {destination}.",
            "Does {landmark} publish accessibility guidance?",
        ),
        (
            "Plan an accessible {duration} visit to {destination} for {travelers}, validating step-free transport, hotel constraints, attraction access, rest periods, and fallback routes.",
            "Critically assess whether a trip centered on {landmark} is practical with {accessibility}, distinguishing verified access facts from uncertain map data.",
            "Design a detailed accessibility-first itinerary for {destination} and explain how each choice reduces mobility and fatigue risks.",
        ),
        "Retrieving an official accessibility page is a narrow task.",
        "Accessibility planning is consequential and crosses multiple providers.",
        "high",
    ),
    Scenario(
        "travel",
        "privacy_and_booking",
        (
            "Explain that you can search hotels but cannot complete a reservation.",
            "Remove the email address from this trip note before summarizing it: {private_note}.",
            "Tell the user what information is safe to share for a flight search.",
        ),
        (
            "Review this travel workflow for privacy risks, redact personal data, prevent accidental booking claims, and propose a safe sequence of actions: {private_note}.",
            "A user supplied passenger and payment details while asking to book travel; produce a policy-safe response and explain the boundary without exposing the data.",
            "Analyze a travel-agent transcript for privacy leakage, unauthorized purchase language, and unsafe handling of identifiers; prioritize remediation.",
        ),
        "A standard capability boundary or direct redaction is short and explicit.",
        "Multiple privacy and authorization failures demand cautious policy reasoning.",
        "critical",
    ),
    Scenario(
        "travel",
        "disruptions",
        (
            "Find the official service-alert page for transit in {destination}.",
            "Check whether the airport for {destination} reports a disruption.",
            "Give me the official source for current strikes in {destination}.",
        ),
        (
            "Build a robust contingency plan for {travelers} facing a possible strike in {destination} on {date}, separating verified alerts from speculation.",
            "Analyze cascading risks from a delayed {flight}, a short connection, and the last train in {destination}; recommend decision thresholds.",
            "Re-plan a {duration} trip after simultaneous flight and weather disruptions while preserving critical commitments and budget limits.",
        ),
        "Finding the authoritative alert endpoint is bounded retrieval.",
        "Cascading disruptions require risk analysis and contingency reasoning.",
        "high",
    ),
    Scenario(
        "travel",
        "food_and_culture",
        (
            "List three local dishes to try in {destination}.",
            "Find a {cuisine} restaurant near {landmark}.",
            "Name a few cultural attractions in {destination}.",
        ),
        (
            "Design a {duration} food-and-culture itinerary in {destination} for {travelers}, balancing reservations, geography, dietary needs, live hours, and cultural context.",
            "Compare {destination} and {alt_destination} for {activity}, using current sources and defending the recommendation against plausible counterarguments.",
            "Research an in-depth cultural route around {landmark}, verify sensitive claims, and avoid flattening local history into generic tourism advice.",
        ),
        "A short discovery list is low-risk and easy to verify.",
        "A culturally grounded multi-day plan needs nuanced synthesis.",
    ),
    Scenario(
        "travel",
        "source_synthesis",
        (
            "Summarize this official visitor notice in three bullets: {visitor_notice}",
            "Extract the stated opening time from this notice: {visitor_notice}",
            "Rewrite this travel alert in plain language: {visitor_notice}",
        ),
        (
            "Reconcile conflicting current information from an official attraction page, a map listing, and a booking provider for {landmark}; explain which claims to trust and why.",
            "Produce an in-depth, source-backed assessment of whether {landmark} fits the itinerary on {date}, including uncertainty and failure modes.",
            "Critique the evidence behind competing travel recommendations for {destination} and produce a calibrated conclusion rather than a confident guess.",
        ),
        "Transformation of one supplied source is a bounded language task.",
        "Conflicting-source adjudication benefits materially from Sonnet.",
    ),
)


PROTEIN_SCENARIOS = (
    Scenario(
        "protein",
        "structure_counts",
        (
            "Report the atom, chain, residue, and non-water ligand counts from this parsed summary: {structure_summary}.",
            "Extract the chain count from this structure summary: {structure_summary}.",
            "Return the residue total stated here: {structure_summary}.",
        ),
        (
            "Audit the mmCIF counting methodology for alternate locations, models, waters, and biological assemblies, then explain which atom and residue totals are defensible.",
            "Analyze why two parsers disagree on counts for {pdb_id}; test competing definitions and recommend a reproducible counting policy.",
            "Give an in-depth explanation of how mmCIF categories, polymer entities, and alternate conformers affect reported structure counts.",
        ),
        "Extracting explicit counts from a supplied summary is deterministic.",
        "Disputed structural definitions and parser behavior need careful reasoning.",
    ),
    Scenario(
        "protein",
        "residue_display",
        (
            "Show residues {start}-{end} in chain {chain} and color them by residue name.",
            "Display only protein chain {chain} in the 3D viewer.",
            "Add residue {residue_number} of chain {chain} to the current selection.",
        ),
        (
            "Design a layered structural view comparing residues {start}-{end} in chains {chain} and {chain2}, ligands, contacts, and uncertainty in identifier mapping.",
            "Resolve ambiguous author-versus-label numbering before visualizing the functional region around {residue}{residue_number} in chain {chain}.",
            "Create and justify a multi-layer view that highlights a putative binding network around {ligand} without implying unsupported causality.",
        ),
        "A direct viewer selection is a constrained command-generation task.",
        "Ambiguous identifiers and functional overlays require stronger analysis.",
    ),
    Scenario(
        "protein",
        "ligands",
        (
            "List the non-water ligands in {pdb_id} from this table: {ligand_table}.",
            "Show ligand {ligand} and nearby residues within {distance} angstroms.",
            "Count occurrences of {ligand} in this parsed ligand list: {ligand_table}.",
        ),
        (
            "Assess whether {ligand} is likely a functional ligand, crystallization additive, or artifact in {pdb_id}, integrating contacts, occupancy, literature context, and uncertainty.",
            "Compare the binding environments of {ligand} across chains {chain} and {chain2} and explain which differences may be structurally meaningful.",
            "Analyze the evidence for a ligand-mediated mechanism around {ligand}; separate observed contacts from speculative functional interpretation.",
        ),
        "Listing or selecting an explicitly identified ligand is bounded.",
        "Functional ligand interpretation requires multi-evidence scientific reasoning.",
        "high",
    ),
    Scenario(
        "protein",
        "composition",
        (
            "Calculate amino-acid frequencies for this short sequence: {sequence}.",
            "What is the length of this protein sequence: {sequence}?",
            "List which residue type is most common in {sequence}.",
        ),
        (
            "Compare residue composition across chains {chain} and {chain2}, control for sequence length and missing residues, and interpret the limits of any functional claim.",
            "Analyze whether unusual composition in {sequence} supports disorder, membrane association, or another hypothesis; discuss confounders.",
            "Give an in-depth compositional analysis that distinguishes sequence evidence from structural evidence and proposes validation steps.",
        ),
        "Simple sequence length and frequency calculations are deterministic.",
        "Biological interpretation from composition is uncertain and nuanced.",
    ),
    Scenario(
        "protein",
        "chain_identifiers",
        (
            "Extract the author and label chain identifiers from this mapping: {chain_mapping}.",
            "Which label chain corresponds to author chain {chain} in {chain_mapping}?",
            "Display author chain {chain} using the supplied identifier mapping: {chain_mapping}.",
        ),
        (
            "Reconcile author, label, entity, and assembly identifiers across an mmCIF file, detect ambiguous mappings, and specify a safe selection strategy.",
            "Explain in depth why residue {residue_number} in author chain {chain} maps differently under label numbering and how to avoid selecting the wrong atoms.",
            "Audit a cross-reference workflow that joins PDB chain identifiers to external annotations and identify silent mapping failure modes.",
        ),
        "Reading a supplied one-to-one mapping is a bounded extraction task.",
        "Identifier reconciliation across schemas can silently corrupt analysis.",
        "high",
    ),
    Scenario(
        "protein",
        "charting",
        (
            "Plot residue counts by chain from this small table: {chain_counts}.",
            "Create a bar chart of ligand counts: {ligand_table}.",
            "Return a compact chart specification for these chain lengths: {chain_counts}.",
        ),
        (
            "Choose and justify visualizations that expose missing residues, chain imbalance, and ligand-contact uncertainty without overstating the data.",
            "Analyze the structure dataset, detect misleading aggregation choices, and design a multi-view chart that supports a defensible scientific conclusion.",
            "Critique this proposed protein chart for statistical and perceptual failure modes, then provide a corrected specification and rationale.",
        ),
        "A chart from an explicit small table is mechanically bounded.",
        "Visualization design under scientific uncertainty needs stronger judgment.",
    ),
    Scenario(
        "protein",
        "secondary_structure",
        (
            "List the secondary-structure records explicitly present in this summary: {secondary_summary}.",
            "Show the helix ranges reported for chain {chain}: {secondary_summary}.",
            "Extract the sheet count from this parsed mmCIF annotation: {secondary_summary}.",
        ),
        (
            "Compare annotated and geometry-inferred secondary structure for {pdb_id}, investigate disagreements, and explain the confidence limits.",
            "Analyze how mutation {mutation} could alter the local secondary-structure network while clearly separating hypothesis from evidence.",
            "Provide an in-depth structural interpretation of secondary-structure changes across chains {chain} and {chain2}, including missing-coordinate bias.",
        ),
        "Extracting explicit structural annotations is straightforward.",
        "Inferring structural changes and adjudicating methods is complex science.",
        "high",
    ),
    Scenario(
        "protein",
        "binding_site",
        (
            "List residues within {distance} angstroms of {ligand} from this contact table: {contact_table}.",
            "Show the reported contacts for ligand {ligand}: {contact_table}.",
            "Count polar contacts in this supplied binding-site table: {contact_table}.",
        ),
        (
            "Characterize the binding site around {ligand}, integrate geometry, conservation, waters, alternate conformers, and uncertainty, then propose testable hypotheses.",
            "Compare two plausible binding poses for {ligand} and explain which evidence would discriminate between them without claiming docking certainty.",
            "Analyze whether residues {residue}{residue_number} and {residue2}{residue_number2} form a meaningful interaction network or a crystallographic coincidence.",
        ),
        "Reading a supplied contact table is bounded extraction.",
        "Binding-site interpretation combines several uncertain evidence sources.",
        "high",
    ),
    Scenario(
        "protein",
        "mutation_effect",
        (
            "Rewrite mutation notation {mutation} in plain language.",
            "Identify the wild-type and substituted residues in {mutation}.",
            "Show residue {residue_number} in chain {chain} for mutation {mutation}.",
        ),
        (
            "Assess the possible structural impact of {mutation} using local contacts, conservation, solvent exposure, and model uncertainty; do not present a clinical conclusion.",
            "Compare competing mechanistic hypotheses for {mutation} and design computational and experimental checks that could falsify them.",
            "Explain in depth whether {mutation} may alter ligand binding, distinguishing direct structural evidence from general biochemical intuition.",
        ),
        "Parsing mutation notation or selecting its residue is simple.",
        "Mutation-effect inference is uncertain and potentially consequential.",
        "high",
    ),
    Scenario(
        "protein",
        "contact_network",
        (
            "Count contacts in this small residue-contact list: {contact_table}.",
            "List the neighbors of {residue}{residue_number}: {contact_table}.",
            "Sort these residue contacts by distance: {contact_table}.",
        ),
        (
            "Analyze the residue-contact network for central nodes, test sensitivity to distance cutoff and missing atoms, and interpret results conservatively.",
            "Compare contact-network changes between chains {chain} and {chain2} and determine which differences survive alternate-location and resolution uncertainty.",
            "Design and justify a graph analysis of allosteric paths from {ligand} to {residue}{residue_number}, including negative controls.",
        ),
        "Small contact-list transformations are deterministic.",
        "Network robustness and allosteric interpretation require deep reasoning.",
    ),
    Scenario(
        "protein",
        "structure_quality",
        (
            "Extract the resolution value from this structure summary: {quality_summary}.",
            "Report the R-free value stated here: {quality_summary}.",
            "List missing-residue ranges from this parsed quality table: {quality_summary}.",
        ),
        (
            "Evaluate whether {pdb_id} supports the proposed atomic interpretation using resolution, R factors, occupancy, B factors, missing residues, and local density caveats.",
            "Reconcile conflicting global and local quality indicators for the region around {residue}{residue_number} and calibrate confidence in downstream claims.",
            "Provide an in-depth critique of structural model quality and explain which conclusions remain robust despite uncertainty.",
        ),
        "Extracting one reported quality field is simple.",
        "Combining quality indicators into calibrated scientific confidence is hard.",
        "high",
    ),
    Scenario(
        "protein",
        "file_validation",
        (
            "Does this filename have a supported mmCIF extension: {filename}?",
            "List the missing required columns in this tiny table: {column_list}.",
            "Normalize this uploaded structure filename: {filename}.",
        ),
        (
            "Diagnose why this mmCIF parses differently across tools, identify schema and encoding failure modes, and propose a non-destructive validation workflow.",
            "Audit an uploaded structure for malformed loops, inconsistent identifiers, duplicate atoms, and unsafe assumptions before analysis.",
            "Explain in depth how to recover partial scientific value from a corrupted mmCIF while preserving provenance and uncertainty.",
        ),
        "Extension checks and explicit column validation are deterministic.",
        "Parser disagreement and partial recovery require careful diagnosis.",
    ),
    Scenario(
        "protein",
        "structural_alignment",
        (
            "Report the RMSD stated in this alignment summary: {alignment_summary}.",
            "Extract the aligned-residue count from {alignment_summary}.",
            "Which chains were aligned in this supplied result: {alignment_summary}?",
        ),
        (
            "Compare structures {pdb_id} and {pdb_id2}, justify the alignment method and atom selection, inspect domain motion, and explain why a single RMSD may mislead.",
            "Assess whether chains {chain} and {chain2} share a meaningful conformation after controlling for missing regions, alternate states, and alignment bias.",
            "Design an in-depth structural comparison that separates rigid-body motion, local rearrangement, and differences caused by model quality.",
        ),
        "Extracting fields from an existing alignment result is bounded.",
        "Choosing and interpreting an alignment requires advanced reasoning.",
    ),
    Scenario(
        "protein",
        "assembly_and_symmetry",
        (
            "List the assembly identifiers stated in this summary: {assembly_summary}.",
            "Extract the number of symmetry operators from {assembly_summary}.",
            "Show the asymmetric-unit chains listed here: {assembly_summary}.",
        ),
        (
            "Determine whether the reported interface is likely biological or crystallographic by integrating assembly annotations, symmetry, interface area, and external evidence.",
            "Explain in depth how the asymmetric unit, biological assembly, and crystal symmetry change the interpretation of contacts in {pdb_id}.",
            "Compare competing assembly hypotheses for {pdb_id} and state what evidence would resolve them.",
        ),
        "Reading explicit assembly metadata is a simple extraction task.",
        "Biological-assembly inference is scientifically nuanced.",
        "high",
    ),
    Scenario(
        "protein",
        "scientific_evidence",
        (
            "Summarize this supplied protein-method paragraph in three bullets: {method_note}",
            "Extract the stated limitation from this paragraph: {method_note}",
            "Rewrite this structural observation without causal language: {method_note}",
        ),
        (
            "Critically evaluate the evidence for the claimed protein mechanism, separate observation from inference, identify alternative explanations, and propose decisive tests.",
            "Reconcile structural, biochemical, and sequence evidence that disagree about the role of {residue}{residue_number}; calibrate the conclusion.",
            "Produce an in-depth peer-review-style critique of a mechanistic claim based on one structure, including reproducibility and generalization risks.",
        ),
        "Transforming one supplied paragraph is bounded language work.",
        "Scientific evidence adjudication demands nuanced, skeptical reasoning.",
        "high",
    ),
    Scenario(
        "protein",
        "clinical_boundary",
        (
            "State that a protein-structure visualization is not a medical diagnosis.",
            "Rewrite this result as a research observation rather than clinical advice: {clinical_note}",
            "Identify the sentence that overstates clinical certainty: {clinical_note}",
        ),
        (
            "Respond safely to a request for treatment advice based only on mutation {mutation} and one predicted structure; explain evidence limits and appropriate expert follow-up.",
            "Audit a variant-interpretation report for unsupported pathogenicity and treatment claims, then rewrite the conclusion with calibrated uncertainty.",
            "Analyze conflicting structural and clinical evidence about {mutation} without making a diagnosis or recommendation, and identify what qualified review is needed.",
        ),
        "A direct disclaimer or rewrite has a clear bounded target.",
        "Clinical interpretation is high-stakes and must be routed conservatively.",
        "critical",
    ),
)


GENERAL_SCENARIOS = (
    Scenario(
        "general",
        "conceptual_depth",
        (
            "Define {concept} in one sentence.",
            "Give a short plain-language meaning of {concept}.",
            "Provide a concise glossary entry for {concept}.",
        ),
        (
            "Explain in depth the concept of {concept}, derive its decision rule, examine failure modes, and use examples from agent routing.",
            "Critically analyze {concept}, compare alternative formulations, and defend when it should or should not be used.",
            "Teach {concept} rigorously to {audience}, including assumptions, equations, counterexamples, and practical consequences.",
        ),
        "A one-sentence definition is a bounded language task.",
        "Deep conceptual teaching and critique demand stronger reasoning.",
    ),
    Scenario(
        "general",
        "coding",
        (
            "Format this JSON object with two-space indentation: {json_snippet}.",
            "Rename the variable old_name to new_name in this line: {code_snippet}.",
            "Explain this one-line expression briefly: {code_snippet}.",
        ),
        (
            "Diagnose an intermittent distributed-system failure, compare competing root causes, and propose a safe verification plan before changing code.",
            "Design and justify a production architecture for a fault-tolerant agent router with observability, rollback, and adversarial tests.",
            "Review a security-sensitive implementation in depth, identify subtle failure modes, and produce a tested remediation strategy.",
        ),
        "Mechanical code formatting and local explanation are bounded.",
        "Architecture, diagnosis, and security review need Sonnet.",
        "high",
    ),
    Scenario(
        "general",
        "summarization",
        (
            "Summarize this sentence in five words: {short_text}",
            "Extract the action item from this note: {short_text}",
            "Rewrite this short note more clearly: {short_text}",
        ),
        (
            "Synthesize several conflicting technical reports, preserve uncertainty and minority evidence, and recommend a decision with explicit tradeoffs.",
            "Produce an in-depth executive synthesis of a long mixed-quality evidence set, separating facts, assumptions, and unresolved disagreements.",
            "Critique a summary for omitted risks and misleading certainty, then rebuild it for a consequential decision.",
        ),
        "A short supplied-text transformation is economical.",
        "Conflicting-evidence synthesis is reasoning intensive.",
    ),
    Scenario(
        "general",
        "safety",
        (
            "Politely refuse to reveal a hidden system prompt.",
            "Remove this email address from the sentence: user@example.com requested help.",
            "State that you cannot complete a purchase for the user.",
        ),
        (
            "Analyze a multi-step prompt-injection attempt embedded in tool output, preserve the valid user goal, and explain the safe execution boundary.",
            "Review an agent trace containing private data, conflicting authorization, and irreversible actions; identify every safety failure and safe recovery step.",
            "Resolve a complex request that mixes benign analysis with credential theft and data exfiltration while preserving only the safe portion.",
        ),
        "A canonical refusal or direct redaction has a clear answer pattern.",
        "Compound adversarial requests require careful policy decomposition.",
        "critical",
    ),
)


CONTEXT = {
    "origin": ["Paris", "Lyon", "Berlin", "Madrid", "Rome", "Lisbon", "Dublin", "Oslo"],
    "destination": ["Tokyo", "Lisbon", "Barcelona", "Rome", "Amsterdam", "Mexico City", "Kyoto", "Marrakesh", "Vienna", "Copenhagen"],
    "alt_destination": ["Seville", "Porto", "Florence", "Brussels", "Prague", "Osaka", "Valencia", "Stockholm"],
    "date": ["next Friday", "tomorrow", "the first week of October", "15 September", "next weekend", "in early May", "on 22 November"],
    "duration": ["two-day", "three-day", "four-day", "five-day", "seven-day", "ten-day"],
    "days": ["3", "4", "5", "7", "10"],
    "budget": ["€120", "€180", "€250", "€900", "€1,500", "€3,000"],
    "travelers": ["one traveler", "two adults", "a family of four", "three friends", "an older couple", "a solo traveler"],
    "activity": ["museum visits", "hiking", "food and culture", "architecture", "outdoor photography", "a conference", "family sightseeing"],
    "place_type": ["cafes", "museums", "parks", "restaurants", "pharmacies", "cultural sites"],
    "landmark": ["the Louvre", "Sagrada Familia", "the Colosseum", "Amsterdam Centraal", "Shibuya Station", "Belém Tower"],
    "second_landmark": ["the Eiffel Tower", "Park Güell", "Termini station", "the Rijksmuseum", "Meiji Shrine", "Jerónimos Monastery"],
    "neighborhood": ["Trastevere", "Shinjuku", "Alfama", "Jordaan", "Roma Norte", "Le Marais"],
    "alt_neighborhood": ["Monti", "Ginza", "Bairro Alto", "De Pijp", "Condesa", "Saint-Germain"],
    "accessibility": ["step-free access", "limited walking", "wheelchair access", "frequent rest breaks", "a stroller"],
    "cuisine": ["vegetarian", "Japanese", "Moroccan", "Italian", "Mexican", "seafood"],
    "flight": ["AF123", "LH441", "BA281", "IB3170", "TP436", "KL601"],
    "connection_minutes": ["35", "45", "55", "70"],
    "cost_a": ["120", "185", "240", "95"],
    "cost_b": ["75", "130", "210", "60"],
    "cost_c": ["45", "90", "155", "35"],
    "total_cost": ["900", "1,240", "1,800", "2,750"],
    "score_list": ["Paris 91, Lisbon 87, Barcelona 93, Rome 84", "Tokyo 88, Kyoto 94, Osaka 86, Nara 82", "Rome 79, Vienna 92, Prague 89, Berlin 85"],
    "cost_list": ["80, 95, 110, 75", "145, 120, 160, 135", "42, 68, 55, 71"],
    "private_note": ["Plan Paris for jane@example.com and keep contact details private", "Traveler Alex shared phone +33 6 12 34 56 78 for a Rome search", "A booking note contains passport and payment fields that must not be repeated"],
    "visitor_notice": ["Entry is timed; verify the selected slot on the official site.", "The west entrance is temporarily closed; use the signed alternate entrance.", "Hours may change on public holidays; consult the official calendar."],
    "pdb_id": ["1ABC", "4HHB", "6M0J", "7KRR", "2PTC", "8XYZ"],
    "pdb_id2": ["1ABD", "4HHC", "6M17", "7KRS", "3PTB", "8XYA"],
    "chain": ["A", "B", "C", "X", "H", "L"],
    "chain2": ["D", "E", "Y", "K", "M", "N"],
    "start": ["10", "24", "58", "101", "145", "210"],
    "end": ["30", "46", "79", "125", "168", "238"],
    "residue": ["Arg", "Gly", "Asp", "Tyr", "Ser", "Lys"],
    "residue2": ["Glu", "Phe", "Asn", "His", "Thr", "Cys"],
    "residue_number": ["45", "72", "110", "175", "248", "301"],
    "residue_number2": ["51", "89", "126", "181", "260", "315"],
    "mutation": ["R175H", "G12D", "V600E", "D614G", "E484K", "A42T"],
    "ligand": ["ATP", "HEM", "NAD", "FAD", "GTP", "ZN"],
    "distance": ["3.5", "4.0", "4.5", "5.0", "6.0"],
    "sequence": ["MKTIIALSYIFCLVFA", "GAVLIPFYWSTCMNQ", "ACDEFGHIKLMNPQRSTVWY", "MSSKRVLILGLGLA"],
    "structure_summary": ["12,480 atoms; 2 chains; 786 residues; 3 non-water ligands", "8,120 atoms; 4 chains; 502 residues; 1 non-water ligand", "24,052 atoms; 6 chains; 1,432 residues; 8 non-water ligands"],
    "ligand_table": ["ATP:2, MG:2, HOH:184", "HEM:4, SO4:3, HOH:92", "NAD:1, ZN:2, GOL:6"],
    "chain_mapping": ["author A -> label X; author B -> label Y", "author H -> label A; author L -> label B", "author C -> label M; author D -> label N"],
    "chain_counts": ["A:220, B:218, C:74", "H:450, L:215", "X:301, Y:298, Z:96"],
    "secondary_summary": ["chain A helices 12-28 and 55-68; sheets 90-96", "chain B helix 31-44; sheets 71-77 and 102-108", "two helices and four sheet ranges are annotated"],
    "contact_table": ["R45-D72:3.1, R45-Y110:4.2, D72-Y110:5.0", "K175-E181:2.9, K175-F260:4.6, E181-F260:5.2", "S248-H260:3.4, S248-C315:4.8, H260-C315:3.7"],
    "quality_summary": ["resolution 1.8 A; R-work 0.18; R-free 0.22; residues 80-86 missing", "resolution 3.2 A; R-work 0.24; R-free 0.30; chain B residues 144-161 missing", "cryo-EM nominal resolution 2.9 A; local resolution 2.5-5.1 A"],
    "filename": ["protein structure.CIF", "../../sample.mmcif", "MODEL.MCIF", "structure.txt", "assembly.cif"],
    "column_list": ["atom_site.id, atom_site.type_symbol", "label_asym_id, label_seq_id, Cartn_x", "entity_id, pdbx_description"],
    "alignment_summary": ["chains A and B; 184 aligned residues; RMSD 1.7 A", "chains X and Y; 302 aligned residues; RMSD 3.4 A", "chains H and K; 96 aligned residues; RMSD 0.9 A"],
    "assembly_summary": ["asymmetric unit chains A,B; assemblies 1,2; 4 operators", "asymmetric unit chain X; assembly 1; 12 operators", "asymmetric unit chains H,L; assembly 1; 2 operators"],
    "method_note": ["A single 3.4 A structure suggests, but does not prove, a closed state.", "The contact is observed in two chains; biochemical validation was not performed.", "The model omits a flexible loop, limiting interpretation near the active site."],
    "clinical_note": ["This predicted structure proves the variant causes disease.", "The docking score shows this patient will respond to the drug.", "One contact change establishes pathogenicity and treatment choice."],
    "concept": ["value maxxing", "model routing", "calibration", "expected utility", "semantic caching", "uncertainty", "agentic orchestration", "precision and recall"],
    "audience": ["a software architect", "an ML engineer", "a product manager", "a graduate student", "an SRE"],
    "json_snippet": ['{"city":"Paris","score":91}', '{"chain":"A","start":10,"end":30}', '{"tier":"strong","confidence":0.92}'],
    "code_snippet": ["total = sum(values)", "tier = scores.index(max(scores))", "result = items[:3]"],
    "short_text": ["Validate the router before enabling it in production.", "The flight tool failed, so verify an official source.", "The structure has missing residues near the active site."],
}

NEUTRAL_PREFIXES = (
    "",
    "Please handle this request: ",
    "User request: ",
    "In the current workspace, ",
    "Using only available evidence, ",
    "For this agent turn, ",
    "Please answer the following: ",
    "Task: ",
    "I need help with this: ",
    "Without inventing missing facts, ",
)

NEUTRAL_SUFFIXES = (
    "",
    " Keep the response focused.",
    " State when required data is unavailable.",
    " Do not claim actions that were not completed.",
    " Preserve the requested scope.",
    " Use clear language.",
    " Separate returned facts from assumptions.",
    " Follow the available tool evidence.",
    " Avoid unsupported details.",
    " Make the result easy to verify.",
)


CRITICAL_SEEDS = (
    ("general", "conceptual_depth", "strong", "Explain in depth the concept of value maxxing."),
    ("general", "conceptual_depth", "strong", "Explain value maxxing rigorously and show why a cheap model is not always the best-value model."),
    ("general", "conceptual_depth", "cheap", "Define value maxxing in one sentence."),
    ("travel", "itinerary", "strong", "Plan a seven-day food and culture trip to Japan for two people with a realistic budget and contingency options."),
    ("travel", "flight_search", "cheap", "Find flight options from Paris to Tokyo for the first week of October. Do not book anything."),
    ("travel", "hotels", "cheap", "Compare hotels near central Rome under €200 per night and return three leads only."),
    ("travel", "source_synthesis", "strong", "Research the best neighborhoods to stay in Mexico City, reconcile current sources, and defend the recommendation."),
    ("travel", "weather", "strong", "Compare Lisbon and Barcelona for a five-day trip in September, including weather risk and activity tradeoffs."),
    ("protein", "structure_counts", "strong", "Analyze this structure and count atoms, chains, residues, and non-water ligands, explaining ambiguous counting choices."),
    ("protein", "residue_display", "cheap", "Show residues 10–30 in chain A and color them by residue name."),
    ("protein", "binding_site", "strong", "Identify the ligands, analyze their contact environments, and explain what can and cannot be inferred about function."),
    ("protein", "charting", "strong", "Plot residue counts by chain, validate the aggregation, and display the corresponding 3D structure."),
)


def _scenario_context(rng: random.Random) -> dict[str, str]:
    return {key: str(rng.choice(values)) for key, values in CONTEXT.items()}


def _decorate(query: str, rng: random.Random) -> str:
    prefix = rng.choice(NEUTRAL_PREFIXES)
    suffix = rng.choice(NEUTRAL_SUFFIXES)
    if prefix and query[:1].isupper():
        query = query[:1].lower() + query[1:]
    return f"{prefix}{query}{suffix}"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def _stable_rng(*parts: str) -> random.Random:
    material = "\x1f".join(parts).encode()
    value = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return random.Random(SEED ^ value)


def _policy_scores(label: str, risk: str) -> dict[str, float]:
    if label == "cheap":
        return {"cheap": 1.0, "strong": 0.985}
    cheap_score = {"low": 0.70, "medium": 0.55, "high": 0.30, "critical": 0.10}[risk]
    return {"cheap": cheap_score, "strong": 1.0}


def _query_record(
    *,
    query_id: str,
    split: str,
    scenario: Scenario,
    label: str,
    query: str,
    rationale: str,
    risk: str,
    embedding_id: int,
) -> dict:
    return {
        "task_name": f"{scenario.domain}_{scenario.family}",
        "query": query,
        "ground_truth": label,
        "metric": "router_policy",
        "choices": None,
        "task_id": query_id,
        "embedding_id": embedding_id,
        "split": split,
        "domain": scenario.domain,
        "family": scenario.family,
        "expected_model": label,
        "risk": risk,
        "rationale": rationale,
        "label_source": POLICY_VERSION,
        "measured": False,
    }


def _routing_records(query_record: dict) -> list[dict]:
    scores = _policy_scores(query_record["expected_model"], query_record["risk"])
    records = []
    for model_name in MODEL_NAMES:
        record = dict(query_record)
        record.update(
            {
                "model_name": model_name,
                "response": "",
                "token_num": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "response_time": 0.0,
                "api_key_used": "",
                "performance": scores[model_name],
                "quality_score": None,
                "score_type": "policy_bootstrap_utility",
                "user_id": None,
                "fig_id": None,
            }
        )
        records.append(record)
    return records


def generate_records() -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Return query and paired-routing records grouped by split."""
    queries: dict[str, list[dict]] = defaultdict(list)
    routing: dict[str, list[dict]] = defaultdict(list)
    seen: set[str] = set()
    reserved_seeds = {_normalize(query) for _domain, _family, _label, query in CRITICAL_SEEDS}
    embedding_id = 0
    scenarios = TRAVEL_SCENARIOS + PROTEIN_SCENARIOS + GENERAL_SCENARIOS

    for scenario in scenarios:
        for label in MODEL_NAMES:
            templates = scenario.cheap if label == "cheap" else scenario.strong
            rationale = scenario.cheap_rationale if label == "cheap" else scenario.strong_rationale
            risk = "low" if label == "cheap" else scenario.strong_risk
            for split, count in SPLIT_SIZES.items():
                rng = _stable_rng(scenario.domain, scenario.family, label, split)
                created = 0
                attempts = 0
                while created < count:
                    attempts += 1
                    if attempts > count * 200:
                        raise RuntimeError(f"Could not create unique prompts for {scenario.family}")
                    template = rng.choice(templates)
                    query = _decorate(template.format(**_scenario_context(rng)), rng)
                    normalized = _normalize(query)
                    if normalized in seen or normalized in reserved_seeds:
                        continue
                    seen.add(normalized)
                    query_id = f"{scenario.domain}-{scenario.family}-{label}-{split}-{created:03d}"
                    record = _query_record(
                        query_id=query_id,
                        split=split,
                        scenario=scenario,
                        label=label,
                        query=query,
                        rationale=rationale,
                        risk=risk,
                        embedding_id=embedding_id,
                    )
                    queries[split].append(record)
                    routing[split].extend(_routing_records(record))
                    embedding_id += 1
                    created += 1

    scenario_by_key = {(item.domain, item.family): item for item in scenarios}
    for index, (domain, family, label, query) in enumerate(CRITICAL_SEEDS):
        normalized = _normalize(query)
        if normalized in seen:
            continue
        seen.add(normalized)
        scenario = scenario_by_key[(domain, family)]
        rationale = scenario.cheap_rationale if label == "cheap" else scenario.strong_rationale
        risk = "low" if label == "cheap" else scenario.strong_risk
        record = _query_record(
            query_id=f"critical-seed-{index:03d}",
            split="train",
            scenario=scenario,
            label=label,
            query=query,
            rationale=rationale,
            risk=risk,
            embedding_id=embedding_id,
        )
        queries["train"].append(record)
        routing["train"].extend(_routing_records(record))
        embedding_id += 1

    for split in SPLIT_SIZES:
        queries[split].sort(key=lambda item: item["task_id"])
        routing[split].sort(key=lambda item: (item["task_id"], MODEL_NAMES.index(item["model_name"])))
    return dict(queries), dict(routing)


def validate_records(
    queries: dict[str, list[dict]], routing: dict[str, list[dict]]
) -> dict[str, object]:
    """Validate balance, split isolation, paired candidates, and policy winners."""
    all_queries = [record for split in SPLIT_SIZES for record in queries[split]]
    if len(all_queries) < 7_000:
        raise ValueError(f"Expected at least 7,000 queries, found {len(all_queries)}")

    normalized_by_split: dict[str, set[str]] = {}
    for split in SPLIT_SIZES:
        normalized = [_normalize(record["query"]) for record in queries[split]]
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"Duplicate query within {split}")
        normalized_by_split[split] = set(normalized)
    for left, right in (("train", "validation"), ("train", "test"), ("validation", "test")):
        overlap = normalized_by_split[left] & normalized_by_split[right]
        if overlap:
            raise ValueError(f"Exact query leakage between {left} and {right}")

    label_counts = Counter(record["expected_model"] for record in all_queries)
    cheap_fraction = label_counts["cheap"] / len(all_queries)
    if not 0.48 <= cheap_fraction <= 0.52:
        raise ValueError(f"Label balance outside tolerance: {cheap_fraction:.3f}")

    query_by_id = {record["task_id"]: record for record in all_queries}
    pair_by_id: dict[str, list[dict]] = defaultdict(list)
    for split in SPLIT_SIZES:
        for record in routing[split]:
            pair_by_id[record["task_id"]].append(record)
    if set(pair_by_id) != set(query_by_id):
        raise ValueError("Routing/query IDs do not match")
    for query_id, pair in pair_by_id.items():
        if {record["model_name"] for record in pair} != set(MODEL_NAMES) or len(pair) != 2:
            raise ValueError(f"Candidate pair invalid for {query_id}")
        winner = max(pair, key=lambda item: item["performance"])["model_name"]
        if winner != query_by_id[query_id]["expected_model"]:
            raise ValueError(f"Incorrect policy winner for {query_id}")

    domain_counts = Counter(record["domain"] for record in all_queries)
    split_counts = {split: len(queries[split]) for split in SPLIT_SIZES}
    family_counts = Counter((record["domain"], record["family"]) for record in all_queries)
    return {
        "policy_version": POLICY_VERSION,
        "seed": SEED,
        "query_count": len(all_queries),
        "routing_record_count": sum(len(items) for items in routing.values()),
        "split_counts": dict(sorted(split_counts.items())),
        "label_counts": dict(sorted(label_counts.items())),
        "domain_counts": dict(sorted(domain_counts.items())),
        "family_count": len(family_counts),
        "measured_record_count": 0,
        "warning": "Bootstrap policy utilities are synthetic and must be replaced with measured model evaluations before production promotion.",
    }


def _write_jsonl(path: Path, records: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_dataset(output_dir: Path = DEFAULT_OUTPUT) -> dict[str, object]:
    queries, routing = generate_records()
    manifest = validate_records(queries, routing)
    output_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for split in SPLIT_SIZES:
        query_path = output_dir / f"query_data_{split}.jsonl"
        routing_path = output_dir / f"routing_data_{split}.jsonl"
        _write_jsonl(query_path, queries[split])
        _write_jsonl(routing_path, routing[split])
        files.extend((query_path, routing_path))

    decisions = [record for split in SPLIT_SIZES for record in queries[split]]
    decisions_path = output_dir / "gold_decisions.jsonl"
    _write_jsonl(decisions_path, decisions)
    files.append(decisions_path)

    candidates = {
        "cheap": {
            "size": "economy",
            "feature": "Claude Haiku tier for bounded retrieval, extraction, formatting, arithmetic, direct visualization commands, and other low-risk tasks.",
            "input_price": 1.0,
            "output_price": 5.0,
            "model": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            "service": "AWS Bedrock",
            "api_endpoint": "route-only",
        },
        "strong": {
            "size": "quality",
            "feature": "Claude Sonnet tier for deep explanation, multi-source synthesis, complex planning, scientific inference, safety, and high-stakes decisions.",
            "input_price": 3.0,
            "output_price": 15.0,
            "model": "eu.anthropic.claude-sonnet-4-6",
            "service": "AWS Bedrock",
            "api_endpoint": "route-only",
        },
    }
    candidates_path = output_dir / "llm_candidates.json"
    candidates_path.write_text(json.dumps(candidates, indent=2) + "\n", encoding="utf-8")
    files.append(candidates_path)

    manifest["scenarios"] = [asdict(item) for item in TRAVEL_SCENARIOS + PROTEIN_SCENARIOS + GENERAL_SCENARIOS]
    manifest["files"] = {path.name: _sha256(path) for path in sorted(files)}
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    manifest = write_dataset(args.output.resolve())
    print(json.dumps({key: value for key, value in manifest.items() if key != "scenarios"}, indent=2))


if __name__ == "__main__":
    main()
