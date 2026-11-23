"""在最终复核失败时，从实际候选输入恢复可导入的表格与续跑记录。"""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from tempfile import TemporaryDirectory

from .client import TuneError
from .report import REPORT_JSON, STATE, write_json
from .savefile import write_tuned_save


class VerificationFailure(TuneError):
    def __init__(self, reason, frames, metric, options):
        super().__init__(reason)
        self.frames = frames
        self.metric = metric
        self.options = options


@contextmanager
def verifying(frames, metric, **options):
    try:
        yield
    except Exception as exc:
        raise VerificationFailure(str(exc), frames, metric, options) from exc


def keep_failed_candidate(run):
    @wraps(run)
    def wrapped(session):
        try:
            return run(session)
        except VerificationFailure as failure:
            if session.cancelled_requested:
                return 130
            job = session.job
            options = dict(failure.options)
            save = options.pop("save", session.save)
            notes = options.pop("notes", [])
            damage = options.pop("damage", 0)
            path = session.run_dir / "复核失败-最后候选.xlsx"
            # 先写成完整 Excel 再发布，旧的恢复表在新表写好之前仍可读取。
            with TemporaryDirectory(dir=session.run_dir) as folder:
                pending = Path(folder) / path.name
                write_tuned_save(pending, save, failure.frames, battle_seed=session.settings.seed,
                                 damage=damage, summary=[["复核失败，保留的候选轴"],
                                 ["原因", str(failure)], ["复核前搜索评分", failure.metric], *notes],
                                 sheet_name="复核记录", **options)
                pending.replace(path)
            checkpoint = getattr(job, "checkpoint", None) or getattr(job, "state_dict", None) or job.state
            write_json(session.run_dir / STATE, checkpoint())
            session.workbook = path
            session.summary = dict(status="verification_failed", error=str(failure),
                                   candidate_metric=failure.metric, candidate_workbook=str(path))
            write_json(session.run_dir / REPORT_JSON, session.summary)
            message = f"复核失败，最后候选已保存到 {path}。原因：{failure}"
            session.log(message)
            raise TuneError(message) from failure
    return wrapped
