# Antenna: Project Overview

**Automated Monitoring of Insects ML Platform**

Platform for processing and reviewing images from automated insect monitoring stations. Designed for collaborating on multi-deployment projects, maintaining metadata, and orchestrating multiple machine learning pipelines for analysis.

---

## Table of Contents

1. [Introduction](#introduction)
2. [High-Level Overview](#high-level-overview)
   - [For Ecologists & Entomologists](#for-ecologists--entomologists)
   - [For Machine Learning Researchers](#for-machine-learning-researchers)
   - [For Software Developers](#for-software-developers)
3. [Platform Modules](#platform-modules)
   - [Core Data Management](#1-core-data-management-amimain)
   - [Machine Learning Orchestration](#2-machine-learning-orchestration-amiml)
   - [Job Processing System](#3-job-processing-system-amijobs)
   - [User Management & Permissions](#4-user-management--permissions-amiusers)
   - [Data Export](#5-data-export-amiexports)
   - [Label Studio Integration](#6-label-studio-integration-amilabelstudio)
   - [Processing Services](#7-processing-services)
   - [Frontend Application](#8-frontend-application)
4. [Key Features](#key-features)
5. [Data Flow & Workflows](#data-flow--workflows)
6. [Technology Stack](#technology-stack)

---

## Introduction

Antenna is an open-source platform that bridges the gap between ecological field research and artificial intelligence. As camera traps for nocturnal insects generate increasingly large volumes of imagery, Antenna provides the tools to process this data at scale using machine learning, while maintaining the collaborative workflows essential for scientific validation.

The platform serves four interconnected goals:

1. **Collaborative Hub**: A space where entomologists, ecologists, and ML researchers can interact and co-create solutions
2. **ML Deployment**: Enable development and deployment of ML models tailored to ecological research needs
3. **Community Building**: Foster a global community dedicated to advancing insect monitoring using AI
4. **Open Science**: Promote transparency through open-source code, data, and models

---

## High-Level Overview

### For Ecologists & Entomologists

Antenna simplifies the path from raw camera trap images to validated species observations:

| What You Can Do | How Antenna Helps |
|-----------------|-------------------|
| **Manage monitoring stations** | Organize deployments by project, site, and device with geographic coordinates |
| **Process thousands of images** | Queue ML jobs that run detection and classification pipelines automatically |
| **Review AI predictions** | Browse occurrences in gallery view, filter by species or confidence scores |
| **Validate identifications** | Add human identifications that override or confirm ML predictions |
| **Export research data** | Download validated occurrences in CSV or JSON for further analysis |
| **Collaborate with teams** | Invite colleagues with role-based permissions (Identifier, Researcher, Manager) |

The interface is designed to minimize technological barriers. You don't need ML expertise to process images, review predictions, and build a validated dataset of insect observations.

### For Machine Learning Researchers

Antenna provides infrastructure for deploying and testing ML models in real ecological workflows:

| Capability | Technical Details |
|------------|-------------------|
| **Modular pipeline architecture** | Pipelines combine multiple algorithms (detector + classifier) with configurable stages |
| **Processing service API** | FastAPI endpoints (`/info`, `/process`, `/readyz`) for integrating custom models |
| **Standardized data contracts** | Pydantic schemas define request/response formats for interoperability |
| **Category mapping** | Map model output indices to taxonomic identifiers (GBIF, iNaturalist) |
| **Batch processing** | Configurable batch sizes and retry logic for large-scale inference |
| **Ground truth collection** | Human identifications provide labeled data for model improvement |

New models can be deployed as independent services without modifying the core platform. The separation between orchestration (Antenna) and inference (Processing Services) allows experimentation with different model architectures.

### For Software Developers

Antenna is a full-stack application built on proven technologies:

| Layer | Technologies |
|-------|--------------|
| **Backend** | Django 4.2, Django REST Framework, Celery, PostgreSQL |
| **Frontend** | React 18, TypeScript, Vite, TanStack React Query, Tailwind CSS |
| **Infrastructure** | Docker Compose, RabbitMQ, MinIO (S3-compatible), FastAPI |
| **Quality** | pytest, pre-commit hooks, Black/ESLint formatting, OpenAPI schema generation |

The codebase follows Django conventions with clear separation between apps. API endpoints are documented via OpenAPI/Swagger. The frontend uses React Query for server state management with TypeScript for type safety.

---

## Platform Modules

### 1. Core Data Management (`ami.main`)

The foundation of Antenna, managing the domain models that represent the insect monitoring workflow.

#### Models & Concepts

| Model | Purpose | Key Features |
|-------|---------|--------------|
| **Project** | Top-level organizational unit | Member management, feature flags, draft/published status, default filters |
| **Deployment** | A monitoring station | Links device + site + location, S3 data source connection |
| **Site** | Physical research location | Name, description, coordinates |
| **Device** | Camera/sensor hardware | Tracks equipment across deployments |
| **Event** | Temporal grouping of images | Auto-generated from capture timestamps (default 120-min gaps) |
| **SourceImage** | Individual captured image | Path, timestamp, dimensions, checksums |
| **Detection** | Bounding box from ML | Coordinates, confidence score, algorithm reference |
| **Classification** | Species label | Taxon, score, algorithm, whether terminal (final) |
| **Occurrence** | Validated observation | Aggregates detections, tracks determination taxon |
| **Identification** | Human species assignment | User attribution, agreement tracking, withdrawal |
| **Taxon** | Species taxonomy | Hierarchical ranks, parent chain, external IDs (GBIF, iNat) |

#### Key Features

- **Hierarchical taxonomy**: Full tree structure with rank levels (Kingdom → Species) and efficient ancestor queries via `parents_json`
- **Event grouping**: Automatic clustering of images into monitoring sessions based on configurable time gaps
- **Default filters**: Project-level configuration for score thresholds and taxa inclusion/exclusion lists
- **Calculated fields**: Cached counts (detections, occurrences, taxa) auto-updated on data changes
- **Object-level permissions**: Fine-grained access control via django-guardian

---

### 2. Machine Learning Orchestration (`ami.ml`)

Manages the connection between Antenna and external ML processing services.

#### Models

| Model | Purpose |
|-------|---------|
| **Pipeline** | Defines a sequence of ML stages (e.g., detect → classify) |
| **Algorithm** | Individual ML model with version tracking |
| **AlgorithmCategoryMap** | Maps model output indices to Taxon IDs |
| **ProcessingService** | External FastAPI endpoint for model inference |
| **ProjectPipelineConfig** | Per-project pipeline settings and enabling |

#### Key Features

- **Multi-stage pipelines**: Chain algorithms for detect-then-classify workflows
- **Processing service health checks**: Periodic liveness probes with cached status
- **Batch configuration**: Configurable batch sizes per pipeline
- **Skip processed images**: Filter already-analyzed images to avoid redundant work
- **Category mapping**: Translate numeric model outputs to taxonomic names

#### Processing Service API Contract

External services must implement:

```
GET  /info   → Available pipelines, algorithms, category maps
GET  /livez  → Liveness check (always succeeds if service is running)
GET  /readyz → Readiness check (may trigger model loading)
POST /process → Run inference on batch of images
```

---

### 3. Job Processing System (`ami.jobs`)

Asynchronous task execution via Celery for long-running operations.

#### Job Types

| Type | Purpose |
|------|---------|
| **MLJob** | Run ML pipeline on image collection |
| **DataStorageSyncJob** | Sync images from S3 storage source |
| **SourceImageCollectionPopulateJob** | Build collection from deployment images |
| **DataExportJob** | Export occurrence data to file |
| **PostProcessingJob** | Apply post-processing algorithms |

#### Key Features

- **Real-time progress**: Nested stage tracking with percentage completion
- **Structured logging**: Python logging integration with job-specific log capture
- **Retry logic**: Failed jobs can be retried with preserved configuration
- **Cancellation**: Running jobs can be cancelled mid-execution
- **Flower monitoring**: Web UI for Celery task inspection at `/flower/`

---

### 4. User Management & Permissions (`ami.users`)

Authentication and role-based access control.

#### Role Hierarchy

| Role | Permissions |
|------|-------------|
| **BasicMember** | View project data, star images, create/run single-image jobs |
| **Researcher** | BasicMember + create/delete data exports |
| **Identifier** | BasicMember + create/update/delete identifications |
| **MLDataManager** | BasicMember + manage jobs, sync storage, delete occurrences |
| **ProjectManager** | Full administrative access to all project features |

#### Key Features

- **Token authentication**: Stateless auth via djoser for API access
- **Project membership**: Users belong to projects with specific roles
- **Object-level permissions**: django-guardian provides per-object access control
- **Draft project visibility**: Unpublished projects only visible to members and superusers

---

### 5. Data Export (`ami.exports`)

Export validated occurrence data for external analysis.

#### Export Formats

| Format | Contents |
|--------|----------|
| **JSON** | Full occurrence data with nested relationships |
| **CSV** | Tabular format with deployment, taxon, scores, timestamps |

#### Key Features

- **Batch processing**: Handles large datasets without memory issues
- **Job progress tracking**: Export status visible in UI
- **Respects filters**: Exports honor project default filters
- **Verification status**: Includes human identification agreement data

---

### 6. Label Studio Integration (`ami.labelstudio`)

Connect Antenna to Label Studio for collaborative labeling workflows.

#### Key Features

- **Bidirectional sync**: Push Antenna data to Label Studio, pull annotations back
- **Webhook handlers**: Respond to Label Studio events in real-time
- **Multiple data types**: Support for captures, detections, and occurrences
- **Configuration management**: Per-project Label Studio settings

---

### 7. Processing Services

External FastAPI applications that perform ML inference.

#### Architecture

Processing services are standalone containers that:

1. Load ML models (detection, classification)
2. Expose the Antenna API contract endpoints
3. Process image batches and return structured results
4. Run independently of the main Antenna stack

#### Example Pipelines

| Pipeline | Description |
|----------|-------------|
| **ZeroShotObjectDetector** | Detect insects using zero-shot object detection |
| **ZeroShotHFClassifier** | Classify detections using HuggingFace models |
| **ConstantClassifier** | Testing pipeline returning fixed labels |
| **RandomSpeciesClassifier** | Testing pipeline returning random species |

#### Extending

1. Create FastAPI app implementing `/info`, `/process`, `/readyz`
2. Define algorithms with `compile()` and `run()` methods
3. Build Docker image and add to compose stack
4. Register endpoint URL in Antenna UI

---

### 8. Frontend Application

React-based web interface providing access to all platform features.

#### Page Structure

| Section | Features |
|---------|----------|
| **Projects** | Browse, create, configure projects |
| **Deployments** | Manage monitoring stations, sync storage |
| **Sessions** | View temporal event groupings, playback captures |
| **Captures** | Image gallery with filtering, upload interface |
| **Occurrences** | Browse validated observations, filter by species |
| **Species** | Taxonomic browser, occurrence counts |
| **Jobs** | Queue management, progress tracking |
| **Team** | Member management, role assignment |
| **Exports** | Create and download data exports |

#### Key Features

- **Gallery views**: Grid layouts with lazy loading for large image sets
- **Advanced filtering**: Multi-faceted filters for taxa, scores, dates, deployments
- **Real-time updates**: React Query provides automatic cache invalidation
- **Responsive design**: Tailwind CSS for mobile-friendly layouts
- **Session playback**: Timeline view with activity plots for monitoring sessions

---

## Key Features

### Image Processing Pipeline

1. **Upload or sync** images from S3-compatible storage
2. **Automatic event grouping** based on temporal gaps
3. **Queue ML jobs** selecting pipeline and image collection
4. **Real-time progress** tracking as images are processed
5. **Browse results** in occurrence gallery with confidence filtering

### Species Identification Workflow

1. **Machine predictions** assigned to occurrences from ML classification
2. **Human review** via identification interface
3. **Agreement tracking** when users confirm predictions
4. **Determination updates** highest-confidence ID becomes primary
5. **Withdrawal support** for revised identifications

### Collaborative Features

- **Project membership** with role-based permissions
- **Multi-user identification** with attribution
- **Team management** interface for project owners
- **Activity tracking** via job history and logs

### Data Quality

- **Score thresholds** filter low-confidence predictions
- **Taxa lists** include/exclude specific species from views
- **Verification flags** track human-validated occurrences
- **Export metadata** includes verification status and confidence

---

## Data Flow & Workflows

### Complete Processing Workflow

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   S3 Storage    │────▶│   Sync Job      │────▶│  SourceImages   │
│   (MinIO/AWS)   │     │                 │     │                 │
└─────────────────┘     └─────────────────┘     └────────┬────────┘
                                                         │
                                                         ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Event Groups   │◀────│  Auto-Grouping  │◀────│  Group by time  │
│  (Sessions)     │     │  (120-min gaps) │     │                 │
└────────┬────────┘     └─────────────────┘     └─────────────────┘
         │
         ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   ML Job        │────▶│  Processing     │────▶│  Detections +   │
│   (Celery)      │     │  Service (API)  │     │  Classifications│
└─────────────────┘     └─────────────────┘     └────────┬────────┘
                                                         │
                                                         ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Occurrences    │◀────│  Aggregation    │◀────│  Group by       │
│                 │     │                 │     │  Detection      │
└────────┬────────┘     └─────────────────┘     └─────────────────┘
         │
         ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Human Review   │────▶│ Identifications │────▶│  Validated      │
│  (UI Gallery)   │     │                 │     │  Observations   │
└─────────────────┘     └─────────────────┘     └────────┬────────┘
                                                         │
                                                         ▼
                                                ┌─────────────────┐
                                                │  Data Export    │
                                                │  (CSV/JSON)     │
                                                └─────────────────┘
```

### Permission Flow

```
User ──▶ Project Membership ──▶ Role Assignment ──▶ Permission Groups
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
              BasicMember       Identifier       ProjectManager
              (view, star)    (+ identify)      (full access)
```

---

## Technology Stack

### Backend Services

| Component | Technology | Purpose |
|-----------|------------|---------|
| Web Framework | Django 4.2 + DRF | REST API, ORM, Admin |
| Task Queue | Celery + RabbitMQ | Async job processing |
| Database | PostgreSQL | Primary data store |
| Object Storage | MinIO (S3-compatible) | Image file storage |
| Caching | django-cachalot | Automatic query caching |

### Frontend

| Component | Technology | Purpose |
|-----------|------------|---------|
| Framework | React 18 | UI components |
| Language | TypeScript | Type safety |
| Build Tool | Vite | Fast development builds |
| State | TanStack React Query | Server state management |
| Styling | Tailwind CSS | Utility-first CSS |

### ML Services

| Component | Technology | Purpose |
|-----------|------------|---------|
| Framework | FastAPI | High-performance API |
| Schemas | Pydantic | Request/response validation |
| Container | Docker | Isolated model deployment |

### Development

| Tool | Purpose |
|------|---------|
| Docker Compose | Local development environment |
| pre-commit | Linting and formatting hooks |
| pytest | Python testing |
| OpenAPI | API documentation |

---

## Getting Started

See the main [README.md](README.md) for installation and quick start instructions.

For development guidance, refer to [CLAUDE.md](CLAUDE.md) which contains detailed information about:

- Development commands and workflows
- Database schema and query optimization
- Testing conventions
- Important file locations
- Known technical debt

---

## Contributing

Antenna is open-source and welcomes contributions. The project promotes:

- **Open code**: Full source available under open license
- **Open data**: Standardized formats for interoperability
- **Open models**: Processing services can integrate any ML model
- **Community-driven**: Issues and pull requests welcome

---

*For more information, visit the project repository or contact the development team.*
