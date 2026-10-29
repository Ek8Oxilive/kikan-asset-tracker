# Kikan - suivi des actifs et remise de materiel signee
# Copyright (C) 2026 Gabriel Chollet
# Developpe avec l'assistance d'une IA (Claude, Anthropic).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Retention RGPD : duree parametrable, previsualisation et purge des salaries sortis.

La suppression est en deux temps : previsualisation (liste) puis purge explicite
(validee par l'admin dans l'interface). La purge ne porte que sur la liste validee a l'ecran, et
chaque purge est journalisee (nombre et matricules). Un document que le stockage refuse de
supprimer est garde en file d'attente et retente a la purge suivante.
"""
import calendar
import json
import os
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from .. import appsettings
from ..audit import audit
from ..auth import require_roles
from ..db import get_session
from ..models import (
    Account,
    Assignment,
    AssignmentAsset,
    Besoin,
    EmployeeStatus,
    Setting,
    Signature,
    User,
    UserApplication,
)
from ..filestore import delete_document

router = APIRouter(
    prefix="/rgpd", tags=["rgpd"],
    dependencies=[Depends(require_roles("informatique"))],
)

RETENTION_KEY = "retention_apres_sortie_mois"
RETENTION_DEFAUT = 36  # 3 ans
# Plancher : les accuses de restitution signes (A.5.11) ne s'effacent pas au lendemain de la sortie.
RETENTION_MIN = int(os.environ.get("KIKAN_RETENTION_MIN_MOIS", "12"))
PENDING_KEY = "rgpd_documents_a_supprimer"  # references que le stockage a refuse de supprimer


class RetentionSetting(BaseModel):
    mois: int


class PurgeRequest(BaseModel):
    # Identifiants affiches par la previsualisation : seuls ceux-la sont purges (absent : tous les candidats).
    ids: list[int] | None = Field(default=None, max_length=10000)


def _get_retention(session: Session) -> int:
    row = session.get(Setting, RETENTION_KEY)
    if row and row.value and row.value.isdigit():
        return max(RETENTION_MIN, int(row.value))  # une valeur enregistree avant le plancher est relevee
    return max(RETENTION_MIN, RETENTION_DEFAUT)


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def _candidats(session: Session) -> list[User]:
    """Salaries sortis dont la date de sortie + retention est atteinte."""
    retention = _get_retention(session)
    today = date.today()
    out = []
    users = session.exec(
        select(User).where(User.statut == EmployeeStatus.sorti)
    ).all()
    for u in users:
        if u.date_sortie and _add_months(u.date_sortie, retention) <= today:
            out.append(u)
    return out


@router.get("/retention")
def get_retention(session: Session = Depends(get_session)):
    return {"mois": _get_retention(session), "minimum": RETENTION_MIN}


@router.put("/retention")
def set_retention(data: RetentionSetting, session: Session = Depends(get_session),
                  admin: Account = Depends(require_roles("informatique"))):
    if data.mois < RETENTION_MIN:
        raise HTTPException(422, f"Duree de conservation trop courte ({RETENTION_MIN} mois minimum)")
    before = _get_retention(session)
    row = session.get(Setting, RETENTION_KEY)
    if row:
        row.value = str(data.mois)
    else:
        row = Setting(key=RETENTION_KEY, value=str(data.mois))
    session.add(row)
    session.commit()
    audit(admin.username, "rgpd_retention", detail=f"{before} -> {data.mois} mois")
    return {"mois": data.mois, "minimum": RETENTION_MIN}


def _pending_documents(session: Session) -> list[str]:
    try:
        refs = json.loads(appsettings.get(session, PENDING_KEY, "") or "[]")
    except ValueError:
        return []
    return [r for r in refs if isinstance(r, str)]


@router.get("/purge-preview")
def purge_preview(session: Session = Depends(get_session)):
    retention = _get_retention(session)
    candidats = _candidats(session)
    return {
        "retention_mois": retention,
        "count": len(candidats),
        "documents_en_attente": len(_pending_documents(session)),
        "candidats": [
            {"id": u.id, "matricule": u.matricule, "nom": u.nom, "prenom": u.prenom,
             "date_sortie": str(u.date_sortie),
             "suppression_prevue": str(_add_months(u.date_sortie, retention))}
            for u in candidats
        ],
    }


@router.post("/purge")
def purge(data: PurgeRequest | None = None, session: Session = Depends(get_session),
          admin: Account = Depends(require_roles("informatique"))):
    candidats = _candidats(session)
    if data is not None and data.ids is not None:
        # Recalcul a l'instant de la purge, restreint a la liste validee : un salarie devenu candidat
        # depuis la previsualisation n'est pas supprime sans avoir ete montre.
        valides = set(data.ids)
        candidats = [u for u in candidats if u.id in valides]
    supprimes = []
    documents: list[str] = []
    for user in candidats:
        for besoin in session.exec(select(Besoin).where(Besoin.user_id == user.id)).all():
            session.delete(besoin)  # references une remise : supprimes avant elles
        session.flush()
        assignments = session.exec(
            select(Assignment).where(Assignment.user_id == user.id)
        ).all()
        for assignment in assignments:
            # Signatures (et fichiers PDF locaux lies) + liens d'actifs.
            sigs = session.exec(
                select(Signature).where(Signature.assignment_id == assignment.id)
            ).all()
            for sig in sigs:
                if sig.ged_reference:
                    documents.append(sig.ged_reference)  # supprime apres la validation en base
                session.delete(sig)
            for link in session.exec(
                select(AssignmentAsset).where(AssignmentAsset.assignment_id == assignment.id)
            ).all():
                session.delete(link)
            # Sans relation declaree entre les modeles, SQLAlchemy ne garantit pas l'ordre des
            # suppressions : on vide d'abord les enfants (signatures, liens) avant le parent.
            session.flush()
            session.delete(assignment)
        session.flush()
        for ua in session.exec(
            select(UserApplication).where(UserApplication.user_id == user.id)
        ).all():
            session.delete(ua)
        session.flush()
        supprimes.append({"id": user.id, "matricule": user.matricule, "nom": user.nom, "prenom": user.prenom})
        session.delete(user)
    # Les references a supprimer sont enregistrees avec la purge (meme transaction) : un arret ou un
    # stockage indisponible ne laisse pas de PDF orphelin sans trace.
    a_supprimer = list(dict.fromkeys(_pending_documents(session) + documents))
    appsettings.put(session, PENDING_KEY, json.dumps(a_supprimer))
    session.commit()
    if supprimes:
        audit(admin.username, "rgpd_purge", detail=(
            f"{len(supprimes)} salarie(s), {len(documents)} document(s) ; matricules : "
            + ", ".join(s["matricule"] or f"id {s['id']}" for s in supprimes))[:2000])
    # Fichiers supprimes seulement une fois la base validee (volume local, partage SMB ou WebDAV).
    echecs = [ref for ref in a_supprimer if not delete_document(ref)]
    for ref in echecs:
        audit("systeme", "rgpd_document_non_supprime", target=ref, status=503,
              detail="stockage indisponible : nouvel essai a la prochaine purge")
    appsettings.put(session, PENDING_KEY, json.dumps(echecs))
    session.commit()
    return {"supprimes": supprimes, "count": len(supprimes),
            "documents_supprimes": len(a_supprimer) - len(echecs), "documents_en_attente": len(echecs)}
