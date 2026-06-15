# Login Information

## Admin Credentials

Set via environment variables on Render before deploying:

- `ADMIN_EMAIL` — admin account email (default: `admin@example.com`)
- `ADMIN_PASSWORD` — admin account password (**required**, no default)

The `setup_admin` management command reads these on every deploy.

## API Endpoints

### Login
```bash
POST /api/auth/login/
Content-Type: application/json

{
  "username": "<ADMIN_EMAIL>",
  "password": "<ADMIN_PASSWORD>"
}
```

### Logout
```bash
POST /api/auth/logout/
```

### Health Check
```bash
GET /api/health/
```

## Notes
- The admin user is created/updated on every deployment via `setup_admin`
- Django admin panel available at: `/admin/`
