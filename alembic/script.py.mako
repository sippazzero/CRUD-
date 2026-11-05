"""Template used by Alembic to generate versioned schema migrations."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    """Apply schema changes required by the next application version."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Restore the prior schema so the previous application version can run."""
    ${downgrades if downgrades else "pass"}
