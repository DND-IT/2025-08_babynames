# Swiss First Names Analysis - Automated Report Generator

## Description

This script generates a comprehensive analysis of Swiss first names trends, rankings, and evolution. It produces automatic text generation in French, German, and English, ready for publication.

## Data Sources

- **PX Web API**: Newborn names by region and year (automatic download)
- **Swiss Federal Statistical Office**: Total population by birth year (manual URLs)

## Setup

### 1. Required Libraries

```r
library(tidyverse)
library(janitor)
library(lubridate)
library(DT)
library(plotly)
library(viridis)
library(glue)
```

### 2. Directory Structure

```
project/
├── data/
│   └── raw/
├── results/
├── output/
└── script.qmd
```

### 3. Annual Configuration Required

**Each year, you need to update:**

#### Automatic URLs (PX Web - no changes needed)

- Female newborns: Updates automatically
- Male newborns: Updates automatically

#### Manual URLs (Population Data - REQUIRES UPDATE)

You must manually find the new download URLs for:

- `all_female_current`: Current year population data
- `all_male_current`: Current year population data
- `all_female_previous`: Previous year population data (for comparisons)
- `all_male_previous`: Previous year population data (for comparisons)

**How to find URLs:**

1. Go to Swiss Federal Statistical Office website
2. Navigate to Names statistics section
3. Use browser inspector (F12) → Network tab
4. Download the CSV files and copy the direct download URLs
5. Update the `urls_config` list in the configuration section

## Usage

1. Update the URLs in the configuration section
2. Run all chunks sequentially
3. Generated outputs:
   - Interactive visualizations
   - Data tables
   - Automated texts in 3 languages (HTML format)
   - Summary CSV export

## Output Files

- `results/name_analysis_summary_YYYY.csv`: Key metrics summary
- `output/article_fr_YYYY.html`: French article
- `output/article_de_YYYY.html`: German article
- `output/article_en_YYYY.html`: English article

## Script Structure

- **Import**: Data download and processing
- **Chapter 1**: Rankings and podiums
- **Chapter 2**: Historical evolution and leaders
- **Chapter 3**: Biggest changes year-over-year
- **Chapter 4**: Total population analysis
- **Chapter 5**: Complementary analyses (diversity, name length)
- **Chapter 6**: Automated text generation

## Key Features

- Fully automated analysis pipeline
- Dynamic year calculation
- Custom ranking system (no ties)
- Interactive visualizations
- Multi-language text generation
- HTML output ready for CMS
- Reusable year-over-year

## Troubleshooting

- Check encoding for special characters in names
- Verify URLs are accessible
- Ensure data structure matches expected format
- Run encoding check function if names display incorrectly
