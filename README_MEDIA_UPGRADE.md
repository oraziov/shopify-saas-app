Aggiornamento UI/media manager

File inclusi:
- app/main.py
- app/services.py
- app/shopify.py
- app/templates/index.html
- app/static/app.css
- app/static/app.js
- requirements.append.txt

Da aggiungere agli scope Shopify:
- read_products
- write_products
- read_files
- write_files
- read_metaobjects
- read_metafields
- write_metafields

Dopo avere cambiato gli scope, reinstalla l'app OAuth.
