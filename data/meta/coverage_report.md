# Document collection and coverage report

## Collection

- Files: **183**, unique documents after de-duplication: **180**
- Total size: 1136 MB
- Categories: Building Agents (28), Applications: Science (27), Evaluating Agents (23), Fundamentals (15), Applications: Simulations (14), Challenges: Safety and Security (14), (Design for) Human-AI Interaction (7), Emergent Abilities and World Models (6), Retrieval-Augmented Generation (5), Challenges: Other (5), Prompt Engineering/Context Engineering (4), Self-improvement/Evolution (4), AI Agent Frameworks (4), Applications: Software Development (4), Applications: Healthcare (3), Applications: Data Science (3), Applications: Analytics (2), Applications: Games (2), Applications: Security (2), Applications: Computer Use (2), Ethics (2), Applications: Education (1), Applications: Synthetic Data (1), Applications: Decision-Making (1), Applications: Embodied Agents (1), Applications: Survey Research (1), Applications: Music (1), Applications: Augmented Reality (1)
- Years: 2000 (2), 2002 (1), 2014 (1), 2018 (3), 2019 (3), 2020 (1), 2021 (2), 2022 (7), 2023 (15), 2024 (26), 2025 (67), 2026 (54)

## Document list and file problems

- The sheet uses Num 92 twice, so from row 93 on the sheet's Num is one lower than the file number. The sheet appears to list 182 documents; there are 183.
- **Wrong file:** 012 should be "A warning about ‘model welfare’", but only 25% of the title words occur in its text, so it contains a different document (inspect manually).

## Duplicates

- 017, 037: "ReAct: Synergizing Reasoning and Acting in Language Models" (detected by: exact_file, same_title, same_url, near_text)
- 028, 039: "Precise Zero-Shot Dense Retrieval without Relevance Labels" (detected by: exact_file, same_title, same_url, near_text)
- 043, 058: "Agent Harness for Large Language Model Agents: A Survey" (detected by: exact_file, same_title, near_text)

## Other oddities (flagged automatically; review manually)

| # | Title | Size MB | Pages | Chars/page | Empty pages | Title in text | Flags |
|---|---|---|---|---|---|---|---|
| 003 | Can AI Expand the Human Mind? | 2.17 | 3 | 4705 | 0.0 | 1.0 | very_short |
| 010 | Measuring AI agent autonomy in practice | 0.72 | 29 | 2328 | 0.07 | 1.0 | possibly_non_paper |
| 012 | A warning about ‘model welfare’ | 1.26 | 84 | 2281 | 0.0 | 0.25 | title_not_in_text, possibly_non_paper |
| 018 | What Should We Engineer in Prompts? Training Humans in Requi | 28.45 | 27 | 4089 | 0.0 | 1.0 | very_large |
| 030 | Language Models Do Not Embed Numbers Continuously | 7.87 | 12 | 2864 | 0.33 | 1.0 | many_pages_without_text |
| 033 | A practical guide to building agents | 7.34 | 34 | 880 | 0.09 | 1.0 | possibly_non_paper |
| 034 | Building effective agents | 1.84 | 19 | 1050 | 0.05 | 1.0 | possibly_non_paper |
| 040 | RAAS: LLM Agentic System Architecture Search with GRPO | 5.12 | 10 | 4307 | 0.0 | 1.0 | possibly_non_paper |
| 043 | Agent Harness for Large Language Model Agents: A Survey | 20.19 | 105 | 4025 | 0.0 | 1.0 | very_large, possibly_non_paper, very_long |
| 054 | Lumine: An Open Recipe for Building Generalist Agents in 3D  | 47.43 | 51 | 3489 | 0.0 | 1.0 | very_large |
| 058 | Agent Harness for Large Language Model Agents: A Survey | 20.19 | 105 | 4025 | 0.0 | 1.0 | very_large, possibly_non_paper, very_long |
| 072 | AgentSociety 2: An Integrated Research Environment for Execu | 21.18 | 85 | 3484 | 0.0 | 1.0 | very_large |
| 082 | The Anatomy of a Personal Health Agent | 93.74 | 148 | 2473 | 0.07 | 1.0 | very_large, very_long |
| 091 | ResearchStudio-Reel: Automate the Last Mile of Research from | 32.9 | 25 | 4175 | 0.04 | 1.0 | very_large |
| 096 | SlideGen: Collaborative Multimodal Agents for Scientific Sli | 26.01 | 64 | 3313 | 0.05 | 1.0 | very_large |
| 097 | Clarus: Coordinating Autonomous Research Agents toward Web-S | 24.38 | 28 | 3692 | 0.0 | 1.0 | very_large |
| 098 | PaperBanana: Automating Academic Illustration for AI Scienti | 42.57 | 49 | 2556 | 0.0 | 1.0 | very_large |
| 106 | SCP: Accelerating Discovery with a Global Web of Autonomous  | 27.86 | 106 | 2441 | 0.0 | 1.0 | very_large, very_long |
| 144 | Agents' Last Exam | 23.53 | 40 | 3512 | 0.0 | 1.0 | very_large |
| 152 | CreativityBench: Evaluating Agent Creative Reasoning via Aff | 32.36 | 57 | 3221 | 0.02 | 1.0 | very_large |
| 167 | The Emerged Security and Privacy of LLM Agent: A Survey with | 27.98 | 35 | 4209 | 0.0 | 1.0 | very_large |
| 173 | AI bots are flooding researchers with requests for money and | 0.2 | 2 | 6091 | 0.0 | 0.83 | possibly_non_paper, very_short |
| 176 | Emergence of human-like polarization among large language mo | 45.51 | 72 | 2636 | 0.03 | 1.0 | very_large |
| 182 | The Ethics of Advanced AI Assistants | 16.57 | 273 | 4306 | 0.0 | 1.0 | very_long |
| 183 | We need a new ethics for a world of AI agents | 0.3 | 3 | 6363 | 0.0 | 1.0 | possibly_non_paper, very_short |

## Coverage (retrieval via /v1/context)

**175 of 180 unique documents are retrievable** (97%).
- Found by title query: 174; only by abstract phrase: 1 (012)
- Of these, 1 contain the wrong document (see above), so **the intended content is available for 174 of 180 documents**.
- Rank of first matching chunk: #1: 173, #2: 1, #4: 1

### Missing or unreachable

Neither the exact title nor a verbatim abstract phrase retrieved any chunk of these documents, and no chunk of them appeared in any other response either, so they are most likely not in the index (failed ingestion) rather than just hard to retrieve.

| # | Title | Size MB | Pages | Possible cause | Top returned docs (title query) |
|---|---|---|---|---|---|
| 072 | AgentSociety 2: An Integrated Research Environment for Execu | 21.18 | 85 | unknown (check ingestion log) | 107, 107, 111, 107, 107 |
| 082 | The Anatomy of a Personal Health Agent | 93.74 | 148 | file > 50 MB (likely above the ingestion size limit) | 83, 77, 77, 83, 77 |
| 106 | SCP: Accelerating Discovery with a Global Web of Autonomous  | 27.86 | 106 | unknown (check ingestion log) | 100, 109, 113, 143, 111 |
| 114 | EduAgentQG: A Multi-Agent Workflow Framework for Personalize | 4.89 | 25 | unknown (check ingestion log) | 68, 40, 126, 64, 111 |
| 122 | Cradle: Empowering Foundation Agents towards General Compute | 19.64 | 68 | unknown (check ingestion log) | 53, 53, 53, 46, 182 |

### What the index reveals

- Distinct source files seen in any response: 178 (of 183 files)
- Sources in the index that are not in data/papers: none
- Files never returned by any coverage query: 5 (072, 082, 106, 114, 122)
- Index file names that differ from the local copy (only ':' vs '_'): 2

- Duplicates in the index: for duplicated documents, the returned chunks came from these copies (both copies ingested = wasted top-k slots):

  - ReAct: Synergizing Reasoning and Acting in Languag: 017×2, 037×3
  - Precise Zero-Shot Dense Retrieval without Relevanc: 028×2, 039×3
  - Agent Harness for Large Language Model Agents: A S: 043×2, 058×3
