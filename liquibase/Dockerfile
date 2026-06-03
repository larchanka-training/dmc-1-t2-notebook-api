# Migration runner: Liquibase + the project changelogs, executed as a one-off
# ECS task before the API service rolls out. The app does NOT migrate on boot
# (see .agents/skills/notebook-api/references/liquibase-migrations.md), so schema
# changes are applied here, separately from the API container.
FROM liquibase/liquibase:4.31.1

# Liquibase's default search path is /liquibase (not /liquibase/changelog), so
# point it at where the changelogs live. Then --changelog-file is relative to it
# (e.g. changelog-master.xml), and its relativeToChangelogFile includes resolve.
ENV LIQUIBASE_SEARCH_PATH=/liquibase/changelog
COPY changelog/ /liquibase/changelog/

# Connection + changelog file are supplied at run time via the environment:
#   LIQUIBASE_COMMAND_URL / _USERNAME / _PASSWORD / _CHANGELOG_FILE
# (injected by the ECS migration task definition; see terraform/modules/data).
CMD ["update"]
