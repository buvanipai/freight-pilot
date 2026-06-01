# api/models.py

import os
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
from mapie.regression import CrossConformalRegressor
from sklearn.ensemble import IsolationForest, RandomForestRegressor
from sklearn.preprocessing import LabelEncoder, StandardScaler

MODELS_DIR = os.path.join(os.path.dirname(__file__), '..', 'models')
ANOMALY_PATH = os.path.join(MODELS_DIR, 'anomaly_detector.joblib')
ETA_PATH = os.path.join(MODELS_DIR, 'eta_predictor.joblib')


def _ensure_models_dir():
    os.makedirs(MODELS_DIR, exist_ok=True)


class AnomalyDetector:
    def __init__(self):
        self.model = IsolationForest(contamination=0.15, n_estimators=80, random_state=42)
        self.scaler = StandardScaler()
        self.mode_encoder = LabelEncoder()
        self.status_encoder = LabelEncoder()
        self.is_fitted = False

    def _build_features(self, df: pd.DataFrame, fit_encoders: bool = False) -> np.ndarray:
        now = datetime.now(timezone.utc)

        def to_hours(ts):
            if pd.isnull(ts):
                return 0.0
            if hasattr(ts, 'tzinfo') and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return max((now - ts).total_seconds() / 3600, 0)

        df = df.copy()
        df['hours_elapsed']          = df['created_at'].apply(to_hours)
        df['hours_since_last_update'] = df['last_update'].apply(to_hours)
        df['eta_overrun_hours']       = (df['hours_elapsed'] - df['eta_hours'].fillna(0)).clip(lower=0)
        df['is_unresponsive']         = (df['carrier_status'] == 'unresponsive').astype(int)

        if fit_encoders:
            self.mode_encoder.fit(df['mode'].fillna('unknown'))
            self.status_encoder.fit(df['status'].fillna('unknown'))

        mode_enc   = self.mode_encoder.transform(df['mode'].fillna('unknown'))
        status_enc = self.status_encoder.transform(df['status'].fillna('unknown'))

        features = np.column_stack([
            df['eta_overrun_hours'].values,
            df['hours_since_last_update'].values,
            df['is_unresponsive'].values,
            df['eta_hours'].fillna(0).values,
            mode_enc,
            status_enc,
        ])
        return features

    def fit(self, df: pd.DataFrame) -> None:
        X = self._build_features(df, fit_encoders=True)
        X_scaled = self.scaler.fit_transform(X)
        self.model.fit(X_scaled)
        self.is_fitted = True

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        X = self._build_features(df, fit_encoders=False)
        X_scaled = self.scaler.transform(X)
        return self.model.score_samples(X_scaled)  # negative = more anomalous

    def fit_and_score_all(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, eta_hours, freight_value_usd, weight_tons,
                          mode, status, carrier_status, created_at, last_update
                   FROM shipments
                   WHERE resolved = FALSE AND status != 'delivered'"""
            )
            rows = cur.fetchall()

        if not rows:
            return

        df = pd.DataFrame([dict(r) for r in rows])

        self.fit(df)
        scores = self.predict(df)
        predictions = self.model.predict(self.scaler.transform(self._build_features(df)))

        with conn.cursor() as cur:
            for i, row in df.iterrows():
                is_anomaly = bool(predictions[i] == -1)
                cur.execute(
                    "UPDATE shipments SET anomaly_score = %s, is_anomaly = %s WHERE id = %s",
                    (float(scores[i]), is_anomaly, row['id'])
                )
        conn.commit()

        _ensure_models_dir()
        joblib.dump(self, ANOMALY_PATH)
        print(f"[models] AnomalyDetector fitted on {len(df)} shipments, saved to {ANOMALY_PATH}")

    @classmethod
    def load(cls) -> 'AnomalyDetector':
        if os.path.exists(ANOMALY_PATH):
            obj = joblib.load(ANOMALY_PATH)
            print(f"[models] AnomalyDetector loaded from {ANOMALY_PATH}")
            return obj
        return cls()


class ETAPredictor:
    def __init__(self):
        self.mapie_model = None
        self.mode_encoder = LabelEncoder()
        self.origin_encoder = LabelEncoder()
        self.dest_encoder = LabelEncoder()
        self.is_fitted = False

    def _build_features(self, df: pd.DataFrame, fit_encoders: bool = False) -> np.ndarray:
        df = df.copy()

        if fit_encoders:
            self.mode_encoder.fit(df['mode'].fillna('unknown'))
            self.origin_encoder.fit(df['origin_state'].fillna('unknown'))
            self.dest_encoder.fit(df['destination_state'].fillna('unknown'))

        def safe_encode(encoder, col):
            known = set(encoder.classes_)
            return encoder.transform([v if v in known else encoder.classes_[0] for v in col.fillna('unknown')])

        mode_enc   = safe_encode(self.mode_encoder, df['mode'])
        origin_enc = safe_encode(self.origin_encoder, df['origin_state'])
        dest_enc   = safe_encode(self.dest_encoder, df['destination_state'])

        return np.column_stack([
            mode_enc,
            origin_enc,
            dest_enc,
            df['freight_value_usd'].fillna(0).values,
            df['weight_tons'].fillna(0).values,
            df['hour_of_day'].values,
            df['day_of_week'].values,
        ])

    def _prepare_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add hour_of_day and day_of_week derived from created_at."""
        df = df.copy()

        def to_utc(ts):
            if pd.isnull(ts):
                return datetime.now(timezone.utc)
            if hasattr(ts, 'tzinfo') and ts.tzinfo is None:
                return ts.replace(tzinfo=timezone.utc)
            return ts

        df['created_at'] = df['created_at'].apply(to_utc)
        df['hour_of_day'] = df['created_at'].apply(lambda ts: ts.hour)
        df['day_of_week'] = df['created_at'].apply(lambda ts: ts.weekday())
        return df

    def fit(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT mode, origin_state, destination_state,
                          freight_value_usd, weight_tons,
                          created_at, last_update
                   FROM shipments
                   WHERE status = 'delivered'"""
            )
            rows = cur.fetchall()

        if len(rows) < 50:
            print(f"[models] ETAPredictor: only {len(rows)} delivered shipments — skipping fit")
            return

        df = pd.DataFrame([dict(r) for r in rows])

        df['created_at'] = pd.to_datetime(df['created_at'], utc=True)
        df['last_update'] = pd.to_datetime(df['last_update'], utc=True)
        df['actual_hours'] = (df['last_update'] - df['created_at']).dt.total_seconds() / 3600
        df['hour_of_day'] = df['created_at'].dt.hour
        df['day_of_week'] = df['created_at'].dt.dayofweek
        df = df[df['actual_hours'] > 0]

        # Include resolved exception outcomes as extra calibration examples
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT s.mode, s.origin_state, s.destination_state,
                              s.freight_value_usd, s.weight_tons, s.created_at,
                              eo.actual_eta_hours
                       FROM exception_outcomes eo
                       JOIN shipments s ON s.id = eo.shipment_id
                       WHERE eo.actual_eta_hours IS NOT NULL AND eo.actual_eta_hours > 0"""
                )
                outcome_rows = cur.fetchall()
            if outcome_rows:
                odf = pd.DataFrame([dict(r) for r in outcome_rows])
                odf['created_at'] = pd.to_datetime(odf['created_at'], utc=True)
                odf['hour_of_day'] = odf['created_at'].dt.hour
                odf['day_of_week'] = odf['created_at'].dt.dayofweek
                odf = odf.rename(columns={'actual_eta_hours': 'actual_hours'})
                cols = ['mode', 'origin_state', 'destination_state',
                        'freight_value_usd', 'weight_tons',
                        'hour_of_day', 'day_of_week', 'actual_hours']
                df = pd.concat([df[cols], odf[cols]], ignore_index=True)
                print(f"[models] ETAPredictor: added {len(outcome_rows)} outcome records for calibration")
        except Exception as exc:
            print(f"[models] ETAPredictor: could not load outcomes — {exc}")

        if len(df) < 50:
            print(f"[models] ETAPredictor: fewer than 50 valid rows — skipping fit")
            return

        X = self._build_features(df, fit_encoders=True)
        y = df['actual_hours'].values

        base = RandomForestRegressor(n_estimators=40, max_depth=12, random_state=42)
        self.mapie_model = CrossConformalRegressor(
            estimator=base, confidence_level=0.9, method='plus', cv=3
        )
        self.mapie_model.fit_conformalize(X, y)
        self.is_fitted = True

        _ensure_models_dir()
        joblib.dump(self, ETA_PATH)
        print(f"[models] ETAPredictor fitted on {len(df)} rows with MAPIE conformal intervals, saved to {ETA_PATH}")

    def predict_one(self, shipment: dict) -> tuple:
        """Returns (eta_predicted, eta_lower, eta_upper, eta_confidence)."""
        if not self.is_fitted:
            return (shipment.get('eta_hours', 0.0), None, None, None)

        created_at = shipment.get('created_at')
        if created_at is None:
            created_at = datetime.now(timezone.utc)
        elif created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        df = pd.DataFrame([{
            'mode': shipment.get('mode', 'unknown'),
            'origin_state': shipment.get('origin_state', 'unknown'),
            'destination_state': shipment.get('destination_state', 'unknown'),
            'freight_value_usd': shipment.get('freight_value_usd', 0.0),
            'weight_tons': shipment.get('weight_tons', 0.0),
            'hour_of_day': created_at.hour,
            'day_of_week': created_at.weekday(),
        }])
        X = self._build_features(df, fit_encoders=False)
        y_pred, y_pis = self.mapie_model.predict_interval(X)
        point = float(y_pred[0])
        lower = float(max(0.0, y_pis[0, 0, 0]))
        upper = float(y_pis[0, 1, 0])
        return (point, lower, upper, 0.9)

    def update_predictions(self, conn) -> None:
        if not self.is_fitted:
            return

        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, mode, origin_state, destination_state,
                          freight_value_usd, weight_tons, created_at
                   FROM shipments
                   WHERE status != 'delivered' AND resolved = FALSE"""
            )
            rows = cur.fetchall()

        if not rows:
            return

        df = pd.DataFrame([dict(r) for r in rows])
        df = self._prepare_df(df)
        X = self._build_features(df, fit_encoders=False)
        y_pred, y_pis = self.mapie_model.predict_interval(X)

        with conn.cursor() as cur:
            for i in range(len(df)):
                point = float(y_pred[i])
                lower = float(max(0.0, y_pis[i, 0, 0]))
                upper = float(y_pis[i, 1, 0])
                cur.execute(
                    """UPDATE shipments
                       SET eta_predicted = %s, eta_lower = %s, eta_upper = %s, eta_confidence = %s
                       WHERE id = %s""",
                    (point, lower, upper, 0.9, df.iloc[i]['id'])
                )
        conn.commit()
        print(f"[models] ETAPredictor updated conformal predictions for {len(df)} shipments")

    @classmethod
    def load(cls) -> 'ETAPredictor':
        if os.path.exists(ETA_PATH):
            obj = joblib.load(ETA_PATH)
            # Old pickled models lack mapie_model — force retrain
            if not hasattr(obj, 'mapie_model') or obj.mapie_model is None:
                obj.mapie_model = None
                obj.is_fitted = False
            print(f"[models] ETAPredictor loaded from {ETA_PATH}")
            return obj
        return cls()
