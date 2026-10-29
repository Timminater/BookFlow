import { useLayoutEffect, useState, type MutableRefObject } from 'react';

interface TabIndicatorStyle {
  left: number;
  width: number;
}

export function useTabIndicator(
  tabRefs: MutableRefObject<Record<string, HTMLButtonElement | null>>,
  activeTab: string,
  tabsDependency: unknown,
): TabIndicatorStyle {
  const [tabIndicatorStyle, setTabIndicatorStyle] = useState({
    left: 0,
    width: 0,
  });

  useLayoutEffect(() => {
    const activeButton = tabRefs.current[activeTab];
    if (!activeButton) {
      // Reset before paint when the selected tab no longer exists.
      // oxlint-disable-next-line react/set-state-in-effect
      setTabIndicatorStyle({ left: 0, width: 0 });
      return undefined;
    }

    const updateIndicator = () => {
      const containerRect = activeButton.parentElement?.getBoundingClientRect();
      const buttonRect = activeButton.getBoundingClientRect();
      if (!containerRect) {
        return;
      }

      setTabIndicatorStyle({
        left: buttonRect.left - containerRect.left,
        width: buttonRect.width,
      });
    };

    updateIndicator();
    window.addEventListener('resize', updateIndicator);

    return () => {
      window.removeEventListener('resize', updateIndicator);
    };
    // Tab contents can change while the active key remains the same.
    // oxlint-disable-next-line react/exhaustive-effect-dependencies
  }, [activeTab, tabRefs, tabsDependency]);

  return tabIndicatorStyle;
}
