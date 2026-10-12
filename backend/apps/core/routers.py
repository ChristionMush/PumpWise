class OwnerMigrationRouter:
    """Schema changes run ONLY through the ``owner`` alias (``migrate --database=owner``).

    The runtime role (``default`` / app_user) can never alter the schema or RLS policies.
    """

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == "owner"
