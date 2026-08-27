# Acknowledgements

ProjectContinuity exists because several open-source projects already solved
important parts of engineering continuity well. We thank the maintainers and
contributors of Turritopsis, Cognee, TeamAI CLI, OpenSpec, Graphify, and the MCP
Python SDK for making their work available for study and integration.

The project's design also benefited from repeated independent review focused
on lifecycle correctness: exact identity, stale-write handling, recoverable
promotion, single-writer operation, evidence hygiene, and honest separation of
“implemented”, “installed”, and “observed”. Those reviews materially improved
the result.

Acknowledgement does not transfer authority between projects. Each upstream
continues to own its source and its native domain; ProjectContinuity owns only
the integration seams and product behavior implemented in this repository.
