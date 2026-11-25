from alembic import context
from app.config.settings import RuntimeSettings
from app.logging_config import configure_logging
from app.storage.database import create_database_engine
from app.storage.models import Base


def run_migrations() -> None:
    settings = RuntimeSettings()
    configure_logging(settings.log_level)
    database_url = settings.database_url.get_secret_value()
    if context.is_offline_mode():
        context.configure(url=database_url, target_metadata=Base.metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_database_engine(database_url, create_directory=True)
    try:
        with engine.begin() as connection:
            context.configure(
                connection=connection,
                target_metadata=Base.metadata,
                render_as_batch=connection.dialect.name == "sqlite",
                transactional_ddl=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
