import { createContext } from 'react';
import type { OmopFocus } from '@/components/Patient/omopRows';

export interface MetricContextValue {
  personId?: number;
  /** Set where the OMOP tab is offered: staff and org admins, outside patient mode. */
  onOpenOmopRows?: (focus: OmopFocus) => void;
}

export const MetricContext = createContext<MetricContextValue>({});
export const WearableMetricProvider = MetricContext.Provider;
