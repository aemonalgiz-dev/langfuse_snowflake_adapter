class SchemaConflictError(RuntimeError):
    """An object already exists under a name the adapter needs for something else."""
