# PRomop — Django OMOP Models

A Django project implementing the OMOP Common Data Model (CDM) v6.0 with Oncology and Genomic extensions for cancer clinical trial matching.

## Overview

This project provides Django models for:
- **OMOP Core (CDM v5.4)**: Standard clinical data models including Person, Visit Occurrence, Condition Occurrence, Drug Exposure, Measurement, and Observation tables
- **OMOP Oncology Extension**: Cancer-specific models including Episode, Histology, Cancer Modifiers, and Episode Events
- **OMOP Genomic Extension**: Genomic variant and sequencing data models including Genomic Variant Occurrence, Specimen, Molecular Sequence, and Genomic Test Results

## Features

- Full Django model implementation of OMOP CDM tables with proper foreign key relationships
- Comprehensive admin interface for all models
- Support for SQLite (development) and PostgreSQL (production)
- Proper database table naming matching OMOP standards
- Django migrations for easy database setup

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Run migrations:
```bash
python manage.py migrate
```

3. Create a superuser:
```bash
python manage.py createsuperuser
```

4. Start the development server:
```bash
python manage.py runserver
```

## Models

### OMOP Core Models
- `Vocabulary` - Standardized vocabularies
- `Domain` - High-level classification of concepts  
- `ConceptClass` - Classification of concepts within domains
- `Concept` - Standardized terminologies
- `Person` - Patient demographic information
- `VisitOccurrence` - Healthcare visits
- `ConditionOccurrence` - Diagnoses and conditions
- `DrugExposure` - Medications and treatments
- `Measurement` - Laboratory tests and vital signs
- `Observation` - Clinical facts that don't fit other domains

### OMOP Oncology Extension Models
- `Episode` - Disease episodes for cancer patients
- `EpisodeEvent` - Linking clinical events to episodes
- `CancerModifier` - Cancer-specific modifiers
- `StemTable` - Pre-processing staging table
- `Histology` - Cancer histology information

### OMOP Genomic Extension Models
- `Specimen` - Biological specimens
- `GenomicVariantOccurrence` - Genetic variants
- `MolecularSequence` - DNA/RNA/protein sequences
- `GenomicVariantAnnotation` - Variant annotations
- `GenomicTestResult` - Results from genomic tests

## Usage

Access the Django admin interface at `/admin/` to manage OMOP data. All models are registered with appropriate list views, filters, and search capabilities.

## Database

The project uses standard OMOP CDM table names and structures, making it compatible with existing OMOP datasets and tools.