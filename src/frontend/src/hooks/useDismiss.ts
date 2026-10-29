import { useEffect, useEffectEvent, type RefObject } from 'react';

export const useDismiss = (
  isOpen: boolean,
  refs: RefObject<HTMLElement | null>[],
  onClose: () => void,
) => {
  const handleClose = useEffectEvent(() => {
    onClose();
  });

  const isInside = useEffectEvent((target: Node) =>
    refs.some((ref) => ref.current?.contains(target)),
  );

  useEffect(() => {
    if (!isOpen) {
      return undefined;
    }

    const handleClickOutside = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }

      if (isInside(target)) {
        return;
      }

      handleClose();
    };

    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        handleClose();
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleEscape);

    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [isOpen]);
};
