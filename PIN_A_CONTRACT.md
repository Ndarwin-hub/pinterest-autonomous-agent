# Pin A Universal Trigger Contract

Pin A is a platform-independent trigger/wake layer implemented through GitHub for the current deployment.

## Purpose

A caller sends one Pin A request. GitHub Actions forwards it to the existing Railway Amazon scheduler. Railway remains the owner of the daily ledger and decides whether the 10-batch automation session is allowed to start.

Pin A does not select individual batches and does not bypass /submit.

## Contract

Logical operation: Pin A

Current transport: GitHub Actions workflow_dispatch, with repository_dispatch reserved for future compatible authentication.

Informational headers:
- X-Pin-A-Source
- X-Pin-A-Request-ID
- X-Pin-A-GitHub-Run-ID
- X-Pin-A-Event

The source value is informational. Pin A does not maintain a hard-coded source allow-list.

## Semantics

One accepted Pin A wake starts the existing Railway daily-session mechanism. That mechanism owns the 10 batches and the daily ledger.

Repeated Pin A calls are safe:
- running cycle -> no second cycle
- completed cycle -> no restart
- eligible new cycle -> one new cycle

The existing /submit, Pin [number], image-selection, and other automation paths remain separate.

## Portability

The caller-facing operation is named Pin A rather than being tied to GitHub-specific batch numbers. GitHub is the current transport layer only. A future Railway-native Pin A endpoint can implement the same contract without changing callers.
