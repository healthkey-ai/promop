# Building with Docker

Rename `.env.example` to `.env` and edit with the appropriate variables.

The actual docker-compose file is found at `./docker-compose.yml` or `./docker-compose.dev.yml`.

## Running

### Production

```bash
git pull
docker compose up -d --build
```

### Development

```bash
docker compose -f docker-compose.dev.yml up -d --build
npm start
```

Use `sudo` or add your user to the `docker` group with  `sudo usermod -aG docker $USER`.

To view your web files inside the container, use `docker exec -it promop_web bash`.

## Running the scripts

You can run scripts (i.e., create_test_user.py) using: `docker exec -it promop_web python create_test_user.py`.

## Examine running containers

```bash
docker ps
```

## View logs

```bash
docker compose logs -f web
docker compose logs -f db
```

## Stop everything

```bash
docker compose down
```

## Use a reverse proxy in Apache

```Apache
<VirtualHost *:80>
    ServerName app.domain.com

    ProxyPreserveHost On
    ProxyPass / http://127.0.0.1:8000/
    ProxyPassReverse / http://127.0.0.1:8000/
</VirtualHost>
```

## Backup

You can backup the Postgres database by backing up the volume here: `/var/lib/docker/volumes/promop_postgres_data/_data/`.

Using one of two methods:

**Dump the Database**:

```bash
docker exec -t promop_db pg_dump -U postgres -d yourdbname > backup.sql
```

**Backup the folder**:

```bash
sudo cp -r /var/lib/docker/volumes/promop_postgres_data/_data /path/to/backup/
```
