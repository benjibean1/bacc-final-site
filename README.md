# Ben Avery Clay Crushers - Website

Static website for the Ben Avery Clay Crushers youth shooting sports team.

## Pages

- **Home** - Overview, disciplines, CTA
- **About** - Team mission, philosophy, athlete gallery (placeholder)
- **Schedule** - Practice schedule (placeholder - TBD)
- **Registration** - Full registration form connected to API
- **Contact** - Contact info and message form (mailto)

## Tech Stack

- HTML5 + CSS3 (no framework)
- Vanilla JavaScript
- Google Fonts: Barlow (headings), Inter (body)
- Hosted on GitHub Pages

## Registration API

The registration form posts to: `http://127.0.0.1:9120/api/athletes`

Backend: Flask app with SQLite database at `/opt/bacc/registrations.db`

## Branding

- Primary Blue: `#1A3A6B`
- Accent Orange: `#F47C20`
- Logo: `assets/logo.png`
- Design reference: aztraps.com/arizona

## Development

No build step needed — static files. Just serve the `/website/` directory or push to GitHub Pages.

## Domain

benaveryclaycrushers.org (configured via GoDaddy DNS)
