# RLE Assessment

An IUCN Red List of Ecosystems assessment report using Quarto and Google Earth Engine.

For setup instructions, see the [RLE-Assessment organization README](https://github.com/RLE-Assessment).

## Development

Preview the site:
```
pixi run preview
```

Make changes, preview the changes, and repeat until satisfied.

Render the final HTML and PDF artifacts:
```
pixi run render
```

Commit and push your changes to publish the site.

## Main content files to edit

- `pyproject.toml` — Python dependencies and project metadata
- `_quarto.yml` — Quarto book configuration (title, date, chapters)
- `config/country_config.yaml` - Country level configuration
- `references_country_specific.bib` - Reference bibliography used to assess the country's ecosystems
