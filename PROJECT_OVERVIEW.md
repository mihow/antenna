# Antenna: Project Overview

**Automated Monitoring of Insects ML Platform**

Antenna is an open-source platform that bridges the gap between ecological field research and artificial intelligence. As camera traps for nocturnal insects generate increasingly large volumes of imagery, Antenna provides the tools to process this data at scale using machine learning, while maintaining the collaborative workflows essential for scientific validation.

The platform serves four interconnected goals:

1. **Collaborative Hub**: A space where entomologists, ecologists, and ML researchers can interact and co-create solutions
2. **ML Deployment**: Enable development and deployment of ML models tailored to ecological research needs
3. **Community Building**: Foster a global community dedicated to advancing insect monitoring using AI
4. **Open Science**: Promote transparency through open-source code, data, and models

---

## For Ecologists & Entomologists

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

---

## For Machine Learning Researchers

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

---

## Key Features

### Image Processing Pipeline

- **Upload or sync** images from S3-compatible storage
- **Automatic event grouping** based on temporal gaps
- **Queue ML jobs** selecting pipeline and image collection
- **Real-time progress** tracking as images are processed
- **Browse results** in occurrence gallery with confidence filtering

### Species Identification Workflow

- **Machine predictions** assigned to occurrences from ML classification
- **Human review** via identification interface
- **Agreement tracking** when users confirm predictions
- **Determination updates**: highest-confidence ID becomes primary
- **Withdrawal support** for revised identifications

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

## Getting Started

See the main [README.md](README.md) for installation and quick start instructions.

For detailed technical documentation including database schema, API reference, and development workflows, refer to [CLAUDE.md](CLAUDE.md).
