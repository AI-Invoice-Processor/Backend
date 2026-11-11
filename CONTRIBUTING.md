# Contribuir

## Flujo trunk-based

- `main` es el único trunk y debe permanecer desplegable.
- Todo cambio entra mediante un Pull Request; no se permite hacer push directo a `main`.
- Las ramas de trabajo nacen de `main`, son cortas y se eliminan después del merge.
- Usa nombres `feat/`, `fix/`, `chore/`, `docs/`, `refactor/`, `ci/` o `test/` seguidos de una descripción breve.
- Integra cambios pequeños y frecuentes; usa flags o cambios compatibles si una funcionalidad aún no está terminada.
- El merge preferido es squash, manteniendo un commit Conventional Commit en `main`.

## Conventional Commits

Los commits y títulos de Pull Request deben seguir:

```text
type(scope opcional): descripción
```

Tipos aceptados: `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`, `style` y `test`.

Ejemplo: `feat(invoices): valida archivos antes de procesarlos`.

El workflow `Backend CI` valida automáticamente mensajes, título, Ruff y pruebas.
