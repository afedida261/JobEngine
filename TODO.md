# JobEngine TODO

## Immediate next steps

1. **Interactive testing interface — implemented**

   `python -m jobengine` now exposes named candidate registration, interactive Terra interviews,
   profile review and plan approval, independent Sol searches, and saved-page evaluation. Every
   command retains local run artifacts and explicit failures. See the
   [terminal debugging guide](docs/terminal-debugging.md). Use it for the next handoff audit;
   offline tests do not establish live search quality.

2. **Audit the Terra-to-Sol handoff**

   Establish exactly what information is passed to each stage. Check that important qualifications aren't lost when the resume is summarized into evidence items. Keep source traceability.

3. **Fix retrieval and error classification**

   A blocked HTTP request should not automatically make a discovered job disappear. Support fallback retrieval, and distinguish a genuinely unqualified candidate from an unverified posting or a technical failure.

4. **Improve performance and simplify architecture**

   Once the pipeline produces reliable results, measure where time is spent, reduce redundant calls, introduce concurrency, and then consider simplifying the codebase. Otherwise, you risk optimizing a workflow that isn't working correctly.

## Second product prong: company-facing demo — still wanted

- [ ] Build a separate company-facing demonstration after the applicant onboarding/demo is established.
- [ ] Let a company authorize access to its current and future hiring needs, including information not published on its website.
- [ ] Accept employer-provided job data directly so matching does not depend on web discovery or scraping.
- [ ] Capture the company's actual role priorities, essential versus flexible qualifications, interview questions, and what good answers demonstrate.
- [ ] Match applicants using that richer employer context and their evidence-backed candidate profiles.
- [ ] Design an agent that can autonomously propose and augment role-specific application questions using the company's priorities and existing questions.
- [ ] Explore concrete scenarios, project-specific explanations, tradeoff questions, and follow-ups that encourage thoughtful, substantive applicant answers and make generic AI-generated submissions less useful.
- [ ] Demonstrate a deliberate-application workflow intended to reduce indiscriminate applications and surface applicants with relevant evidence and interest.
- [ ] Define the employer's controls over question publication, changes, and refresh frequency before enabling autonomous external changes.
- [ ] Evaluate answer usefulness, applicant effort/drop-off, accessibility, and whether the questions actually reveal relevant understanding.
- [ ] Keep claims precise: no question is guaranteed to be AI-proof, and answer quality alone does not establish genuine interest or prove authorship. Do not substitute AI-authorship detection for evaluating substance.

## Later, after both demos

- [ ] Decide how employer-authorized sources and open-web discovery coexist.
- [ ] Assess integrations, permissions for unpublished roles, accounts, storage, and deployment based on demonstrated needs.

## Aviv's ideas
- could be good to just completely seperate the agents. One asks questions then hands off information to a new agent which handles search. That way one agent is not overburdened with context.
-- or a reflection agent which critiques the results and loops - but that may be too clunky as well, take too much time. 
- maybe there's some way to introduce mcp in some way

UI Screens: 
3. main navigable user dashboard as described previously. The dashboard should have links to your profile, saved roles, applied roles, suggested opportunities. This should not be a side tab. Rather 3 horizontal lines on the top right that when hovered over opens down into a list of the clickable links which send you to the corresponding pages. Corresponding pages could be designed but are not finalized yet. 

