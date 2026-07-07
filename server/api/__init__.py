# BREADCRUMB: server > api - FastAPI BFF + HTTP endpoints; viewmodel.py is the single source of UI truth (metric catalog, view-models, room graph) rendered by both PWA and panel. Contract: 0013. Parent: server/AGENTS.md.
# REUSE-WHEN: you need to expose or render device/room/control data to a client — author it once in the BFF view-model, never a panel-specific endpoint
