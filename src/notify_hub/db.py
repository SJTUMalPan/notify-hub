"""M2 的数据库访问入口：SQLite 引擎、幂等建表与 Session 上下文。

设计要点：
- 引擎带 ``connect_args={"check_same_thread": False}``，因为后台提醒线程（M6）会访问同一引擎。
- 每个连接开启 ``PRAGMA foreign_keys=ON``。
- ``session()`` 的 sessionmaker 使用 ``expire_on_commit=False``：M6/M7 会在 ``with db.session()``
  内部创建行、退出后继续读 ORM 属性；默认的 ``expire_on_commit=True`` 会抛 ``DetachedInstanceError``。
- ``init_schema()`` 幂等：``create_all`` 对已存在的表是 no-op；部分唯一索引在 ``todos`` 上由
  模型层的 ``Index(...)`` 声明并随之创建。

本文件由 M2 模块负责，见 ``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md``
第 6 节「模块 M2：数据模型与持久化」。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session, SQLModel

from . import models as _models  # noqa: F401  导入以把四张表注册进 SQLModel.metadata

__all__ = ["Database"]

#: SQLite 连接建立时执行的语句：本项目依赖外键约束（todos.message_id 等）。
_PRAGMAS = ("PRAGMA foreign_keys=ON",)


class Database:
    """SQLite 持久化入口（引擎 + 建表 + 事务性 Session）。"""

    def __init__(self, db_path: str | Path, *, echo: bool = False) -> None:
        self._db_path = Path(db_path)
        self._engine = create_engine(
            f"sqlite:///{self._db_path}",
            echo=echo,
            connect_args={"check_same_thread": False},
        )
        event.listen(self._engine, "connect", self._on_connect)
        self._session_factory = sessionmaker(
            bind=self._engine,
            class_=Session,
            expire_on_commit=False,
        )

    # ------------------------------------------------------------------ #
    # 连接钩子
    # ------------------------------------------------------------------ #
    @staticmethod
    def _on_connect(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            for statement in _PRAGMAS:
                cursor.execute(statement)
        finally:
            cursor.close()

    # ------------------------------------------------------------------ #
    # 公开接口（冻结契约）
    # ------------------------------------------------------------------ #
    @property
    def engine(self) -> Engine:
        return self._engine

    def init_schema(self) -> None:
        """幂等建表 + 建索引；``db_path`` 的父目录不存在时自动创建。"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        SQLModel.metadata.create_all(self._engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """yield 一个 ``sqlmodel.Session``。

        正常退出时 commit；异常时 rollback 并原样抛出；无论哪条路径都 close。
        Session 以 ``expire_on_commit=False`` 创建，提交后实例属性仍可读。
        """
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """释放连接池。"""
        self._engine.dispose()
