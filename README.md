# Shopify SaaS FastAPI starter

Starter app FastAPI per Shopify OAuth multi-store.

## Cosa include

- OAuth install flow (`/install`, `/callback`)
- Verifica HMAC su callback
- Salvataggio token offline per shop
- Dashboard demo che legge prodotti e immagini via Admin GraphQL API
- Webhook `app/uninstalled` per rimuovere il token salvato
- Retry di base su errori transitori Shopify
- Logging JSON utile su Render

## Setup locale

1. Crea un virtualenv
2. Installa dipendenze

```bash
pip install -r requirements.txt
```

3. Copia `.env.example` in `.env` e compila i valori
4. Avvia

```bash
uvicorn app.main:app --reload
```

## URL di installazione

```text
https://YOUR-APP-URL/install?shop=your-store.myshopify.com
```

## Render

- imposta `APP_URL` con l'URL pubblico Render
- imposta `SHOPIFY_CLIENT_ID` e `SHOPIFY_CLIENT_SECRET`
- aggiorna nel Dev Dashboard Shopify:
  - App URL
  - Allowed redirection URL: `https://YOUR-APP-URL/callback`
  - Webhook `app/uninstalled`: `https://YOUR-APP-URL/webhooks/app/uninstalled`

## Note pratiche

- Questa base usa SQLite per semplicità. Per produzione reale è meglio Postgres.
- Se la tua app sarà embedded dentro Shopify Admin, aggiungi poi il frontend con App Bridge e session token.
- Il dashboard demo si aspetta un metafield `custom.gallery` contenente un JSON array di media IDs Shopify.
