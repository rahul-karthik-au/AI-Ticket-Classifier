# AI Support Ticket Classifier

An AI-powered system that automatically classifies incoming support tickets into
categories (billing, technical, account, feature-request), assigns a priority
(P1–P4), and routes them to the right team — all in under 500ms.

## Tech stack
AWS Lambda · API Gateway · DynamoDB · Upstash Redis · Claude API · React + TypeScript

## Architecture
Request → API Gateway → Lambda → Redis cache check → Claude API → DynamoDB → Response

## Project status

| Focus                       | Status |
|-----------------------------|--------|
| Foundation and AWS setup    | In progress |
| Claude classification engine | Not started |
| Redis cache and circuit breaker | Not started |
| Batch processing and webhooks | Not started |
| Agent dashboard (React) | Not started |
| Monitoring and observability | Not started |
| Testing | Not started |
| Deployment and polish | Not started |

