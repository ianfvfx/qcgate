"""
TechOps Tools - Slate Detector
==============================
Detects Black Kite slate, duplicate frames, and blanking with confidence scoring.
"""

import time
import threading
import queue
from typing import Optional, Dict, List, Tuple

# Try to import cv2 for video analysis
try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:  # noqa
    HAS_CV2 = False
    cv2 = None
    np = None

app_settings = {'qc_downscale': 0.85}


class SlateDetector:
    """Detects Black Kite slate, duplicate frames, and blanking with confidence scoring."""
    
    BLACK_THRESHOLD = 30
    SHOT_CHANGE_THRESHOLD = 30  # MSE threshold to detect shot/scene changes
    
    # Multi-pass thresholds for blanking detection (wide net → strict)
    BLANKING_THRESHOLDS = [80, 50, 30, 16]  # 4 passes: dark gray → broadcast black
    
    # Confidence thresholds
    CONFIDENCE_RED = 70     # >= 70% = certified blanking (RED flag)
    CONFIDENCE_ORANGE = 50  # 50-69% = probable blanking (ORANGE flag)
    
    def __init__(self, video_path: str, custom_duration: Optional[float] = None, 
                 debug_mode: bool = False, debug_output_dir: Optional[str] = None):
        self.video_path = video_path
        self.custom_duration = custom_duration
        self.fps = 25.0
        self.total_frames = 0
        self.slate_end_frame = 0
        self.slate_duration = 0
        self.duplicate_frames: List[Dict] = []
        self.blanking_segments: List[Dict] = []  # List of blanking segments with timecodes
        self.letterbox_segments: List[Dict] = []  # List of (start_frame, end_frame, baseline) tuples
        
        # Debug mode for blanking analysis
        self.debug_mode = debug_mode
        self.debug_output_dir = debug_output_dir
        self.worst_frame_data = None  # Stores frame with highest blanking confidence
    
    def _is_slate_frame(self, frame) -> bool:
        """Detect if frame matches Black Kite slate pattern (cream top, teal bottom)."""
        h, w = frame.shape[:2]
        top_region = frame[0:int(h*0.4), int(w*0.3):int(w*0.7)]
        bottom_region = frame[int(h*0.7):h, int(w*0.3):int(w*0.7)]
        
        top_mean = np.mean(top_region, axis=(0, 1))
        is_cream = (top_mean[0] > 200 and top_mean[1] > 210 and top_mean[2] > 210 and
                   top_mean[2] > top_mean[0])
        
        bottom_mean = np.mean(bottom_region, axis=(0, 1))
        is_teal = (bottom_mean[1] > bottom_mean[2] and bottom_mean[1] > 100 and bottom_mean[0] > 80)
        
        return is_cream and is_teal
    
    def _is_black_frame(self, frame) -> bool:
        """Check if frame is predominantly black."""
        return np.mean(frame) < self.BLACK_THRESHOLD
    
    def _analyze_blanking_strip(self, gray, edge: str, width: int) -> Dict:
        """Analyze pixel distribution in suspected blanking strip.
        
        Returns histogram concentration, variance, and other stats to help
        distinguish real blanking (uniform broadcast black) from dark content.
        
        Real blanking characteristics:
        - High concentration (>90%) of pixels in broadcast black range [0-16]
        - Very low variance/std_dev (<5) - uniformly dark
        - Mean value close to 0
        
        Dark content characteristics:
        - Lower concentration (<80%) - pixels spread across dark grays
        - Higher variance/std_dev (>8) - not uniform
        - Mean value higher (20+)
        """
        h, w = gray.shape[:2]
        
        if width <= 0:
            return {
                'concentration': 0.0,
                'variance': 999.0,
                'std_dev': 999.0,
                'mean': 255.0,
                'histogram': [],
                'valid': False
            }
        
        # Extract the strip based on edge
        if edge == 'left':
            strip = gray[:, :width]
        elif edge == 'right':
            strip = gray[:, w-width:]
        elif edge == 'top':
            strip = gray[:width, :]
        else:  # bottom
            strip = gray[h-width:, :]
        
        # Histogram: count pixels in broadcast black range [0-16]
        broadcast_black = np.sum(strip <= 16)
        total_pixels = strip.size
        concentration = broadcast_black / total_pixels if total_pixels > 0 else 0.0
        
        # Variance and standard deviation
        strip_float = strip.astype(float)
        variance = float(np.var(strip_float))
        std_dev = float(np.std(strip_float))
        mean_val = float(np.mean(strip_float))
        
        # 16-bin histogram for detailed analysis
        hist, _ = np.histogram(strip, bins=16, range=(0, 256))
        
        return {
            'concentration': concentration,  # % of pixels in [0-16]
            'variance': variance,
            'std_dev': std_dev,
            'mean': mean_val,
            'histogram': hist.tolist(),
            'valid': True
        }
    
    def _detect_blanking_at_threshold(self, gray, threshold: int, scan_depth: int) -> Dict:
        """Detect blanking at a specific threshold.
        
        Returns raw blanking widths (before letterbox subtraction).
        """
        h, w = gray.shape[:2]
        result = {'left': 0, 'right': 0, 'top': 0, 'bottom': 0}
        
        # LEFT edge
        for col in range(scan_depth):
            if np.max(gray[:, col]) <= threshold:
                result['left'] = col + 1
            else:
                break
        
        # RIGHT edge
        for col in range(w - 1, w - scan_depth - 1, -1):
            if np.max(gray[:, col]) <= threshold:
                result['right'] = w - col
            else:
                break
        
        # TOP edge
        for row in range(scan_depth):
            if np.max(gray[row, :]) <= threshold:
                result['top'] = row + 1
            else:
                break
        
        # BOTTOM edge
        for row in range(h - 1, h - scan_depth - 1, -1):
            if np.max(gray[row, :]) <= threshold:
                result['bottom'] = h - row
            else:
                break
        
        return result
    
    def _check_spatial_consistency(self, gray, edge: str, width: int, threshold: int) -> float:
        """Check if blanking has consistent width across the entire edge.
        
        Samples 10 points along the edge and measures width at each.
        Returns consistency score 0.0-1.0 (1.0 = perfectly consistent).
        
        STRICT RULE: Blanking must exist at ALL 10 points with similar width.
        If any point has 0 or highly inconsistent width, returns 0.0.
        """
        h, w = gray.shape[:2]
        
        if width <= 0:
            return 0.0
        
        widths = []
        
        if edge in ['left', 'right']:
            # Sample at 10 points along height
            sample_points = [int(h * i / 11) for i in range(1, 11)]
            for row in sample_points:
                measured = 0
                if edge == 'left':
                    for col in range(min(width + 20, w)):  # Extended search range
                        if gray[row, col] <= threshold:
                            measured = col + 1
                        else:
                            break
                else:  # right
                    for col in range(w - 1, max(w - width - 20, 0) - 1, -1):
                        if gray[row, col] <= threshold:
                            measured = w - col
                        else:
                            break
                widths.append(measured)
        else:  # top or bottom
            # Sample at 10 points along width
            sample_points = [int(w * i / 11) for i in range(1, 11)]
            for col in sample_points:
                measured = 0
                if edge == 'top':
                    for row in range(min(width + 20, h)):
                        if gray[row, col] <= threshold:
                            measured = row + 1
                        else:
                            break
                else:  # bottom
                    for row in range(h - 1, max(h - width - 20, 0) - 1, -1):
                        if gray[row, col] <= threshold:
                            measured = h - row
                        else:
                            break
                widths.append(measured)
        
        if not widths:
            return 0.0
        
        # === STRICT CHECK: ALL 10 points must have blanking ===
        min_width = min(widths)
        if min_width == 0:
            return 0.0  # FAIL: Not a continuous line across entire edge
        
        # === CONSISTENCY CHECK: Widths must be similar ===
        max_width = max(widths)
        
        # Allow tolerance: min must be at least 50% of max
        # (handles slight compression artifacts but catches wild variations)
        if min_width < max_width * 0.5:
            return 0.0  # FAIL: Too much variation (not a uniform blanking line)
        
        # Calculate consistency score: 1.0 if all same, lower if variance
        variance = np.std(widths)
        # Variance of 0 = perfect (1.0), variance of 3+ = poor (0.0)
        consistency = max(0.0, 1.0 - (variance / 3.0))
        
        return consistency
    
    def _get_continuous_blanking_width(self, gray, edge: str, max_scan: int, threshold: int) -> int:
        """Get the continuous blanking width for an edge.
        
        Returns the MINIMUM width found across all 10 sample points.
        If any point has 0, returns 0 (not continuous).
        
        This ensures we only report blanking that exists as a continuous
        line across the entire frame edge.
        """
        h, w = gray.shape[:2]
        widths = []
        
        if edge in ['left', 'right']:
            sample_points = [int(h * i / 11) for i in range(1, 11)]
            for row in sample_points:
                measured = 0
                if edge == 'left':
                    for col in range(min(max_scan, w)):
                        if gray[row, col] <= threshold:
                            measured = col + 1
                        else:
                            break
                else:
                    for col in range(w - 1, max(w - max_scan, 0) - 1, -1):
                        if gray[row, col] <= threshold:
                            measured = w - col
                        else:
                            break
                widths.append(measured)
        else:
            sample_points = [int(w * i / 11) for i in range(1, 11)]
            for col in sample_points:
                measured = 0
                if edge == 'top':
                    for row in range(min(max_scan, h)):
                        if gray[row, col] <= threshold:
                            measured = row + 1
                        else:
                            break
                else:
                    for row in range(h - 1, max(h - max_scan, 0) - 1, -1):
                        if gray[row, col] <= threshold:
                            measured = h - row
                        else:
                            break
                widths.append(measured)
        
        if not widths:
            return 0
        
        min_width = min(widths)
        max_width = max(widths)
        
        # If any point has 0, not continuous
        if min_width == 0:
            return 0
        
        # Find cluster of widths similar to the minimum
        # Real blanking stops at content; inflated values hit scan_depth due to dark content
        # Tolerance: widths within 50% above the minimum, or within 10px
        tolerance = max(min_width * 0.5, 10)  # 50% or 10px, whichever is larger
        cluster = [cw for cw in widths if cw <= min_width + tolerance]
        
        # Need at least 3 points (30%) in the cluster for it to be valid
        if len(cluster) >= 3:
            return min_width
        
        # Fallback: original strict check (all points must be similar)
        if min_width < max_width * 0.5:
            return 0
        
        return min_width
    
    def _check_symmetry(self, blanking: Dict) -> float:
        """Check if blanking is symmetric (L=R or T=B).
        
        Returns 1.0 if symmetric (bonus), 0.0 if asymmetric (no bonus, no penalty).
        """
        left, right = blanking.get('left', 0), blanking.get('right', 0)
        top, bottom = blanking.get('top', 0), blanking.get('bottom', 0)
        
        # Check horizontal symmetry (L and R both present and similar)
        h_symmetric = False
        if left > 0 and right > 0:
            h_symmetric = abs(left - right) <= max(2, min(left, right) * 0.2)  # Within 20% or 2px
        
        # Check vertical symmetry
        v_symmetric = False
        if top > 0 and bottom > 0:
            v_symmetric = abs(top - bottom) <= max(2, min(top, bottom) * 0.2)
        
        # If any edge has blanking and is symmetric, return 1.0 (bonus)
        if h_symmetric or v_symmetric:
            return 1.0
        
        # Asymmetric = no bonus (0.0), but no penalty either
        return 0.0
    
    def _detect_blanking_frame(self, frame, letterbox_baseline=None, gray=None) -> Dict:
        """Detect blanking with multi-pass confidence scoring.
        
        Pass 1: Threshold 80 (wide net - catches dark grays)
        Pass 2: Threshold 50 (medium)
        Pass 3: Threshold 30 (strict)
        Pass 4: Threshold 16 (broadcast black)
        
        Each pass that confirms adds 25% confidence.
        Spatial consistency adds up to 20%.
        Symmetry adds up to 10%.
        
        Returns blanking info with confidence scores.
        """
        h, w = frame.shape[:2]
        if gray is None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        
        result = {'left': 0, 'right': 0, 'top': 0, 'bottom': 0}
        
        # === DARK FRAME REJECTION ===
        # During scene transitions / fades-to-black, the entire frame goes dark.
        # This makes edges appear as "blanking" when it's actually content fading out.
        # Skip blanking detection if the content area (central 80%) is very dark.
        margin_h, margin_w = int(h * 0.1), int(w * 0.1)
        content_region = gray[margin_h:h-margin_h, margin_w:w-margin_w]
        content_mean = float(np.mean(content_region))
        if content_mean < 30:
            # Frame is mostly black (scene transition / fade) - no blanking possible
            result['has_blanking'] = False
            result['confidence'] = 0
            result['confidence_details'] = {}
            result['severity'] = 'none'
            return result
        
        # Scan up to 15% of smaller dimension (catches thick blanking)
        # For 1080p: min(1920,1080) * 0.15 = 162px
        # Capped at 200px max to avoid scanning too much
        scan_depth = min(200, int(min(w, h) * 0.15))
        
        # Get baseline values (letterboxing to subtract)
        lb = letterbox_baseline or {'left': 0, 'right': 0, 'top': 0, 'bottom': 0}
        
        # Minimum blanking: 1px for 1080p and below, 2px for 4K+
        min_blanking = 2 if w >= 3840 else 1
        
        # Maximum blanking: anything larger is content, not blanking
        # Real blanking from encoding/format issues is typically 1-20px
        if w >= 3840:  # 4K
            max_blanking = 40
        elif w >= 1920:  # HD
            max_blanking = 20
        else:  # SD
            max_blanking = 15
        
        # === MULTI-PASS DETECTION WITH CONTINUOUS WIDTH CHECK ===
        edge_confidence = {}
        edge_details = {}
        
        for edge in ['left', 'right', 'top', 'bottom']:
            # Get continuous width at each threshold level
            continuous_widths = []
            for threshold in self.BLANKING_THRESHOLDS:
                cw = self._get_continuous_blanking_width(gray, edge, scan_depth, threshold)
                # Subtract letterbox baseline
                cw = max(0, cw - lb.get(edge, 0))
                continuous_widths.append(cw)
            
            # === WIDTH CONSISTENCY GATE ===
            # Real blanking persists at broadcast black (thresh ≤16).
            # Dark content only appears at loose threshold (80) but vanishes at 16.
            # Only reject if there's ZERO broadcast black — don't reject thin blanking
            # that has adjacent dark content (which inflates width_at_80).
            width_at_80 = continuous_widths[0]
            width_at_16 = continuous_widths[3]
            
            if width_at_80 > 0 and width_at_16 < min_blanking:
                # Nothing at broadcast black level — it's dark content, not blanking
                edge_confidence[edge] = 0
                edge_details[edge] = {
                    'passes': 0, 'spatial': 0, 'symmetry': 0,
                    'rejected': 'width_consistency',
                    'width_at_80': width_at_80,
                    'width_at_16': width_at_16
                }
                continue
            
            # Smart width selection: use broadcast-black width (thresh 16) as primary
            # This is the true blanking width — anything only visible at 80 is dark content
            width = width_at_16 if width_at_16 >= min_blanking else 0
            
            # Fallback: check thresh 30 if thresh 16 missed by 1px
            if width == 0 and continuous_widths[2] >= min_blanking:
                width = continuous_widths[2]
            
            # Check min/max bounds
            if width < min_blanking or width > max_blanking:
                edge_confidence[edge] = 0
                edge_details[edge] = {'passes': 0, 'spatial': 0, 'symmetry': 0, 'rejected': 'size'}
                continue
            
            # === HISTOGRAM + VARIANCE ANALYSIS ===
            # Analyze the blanking strip to distinguish real blanking from dark content
            strip_analysis = self._analyze_blanking_strip(gray, edge, width)
            
            # Rejection criteria: dark content (not true blanking)
            # Real blanking: concentration > 0.85, std_dev < 8, mean < 12
            # Dark content: lower concentration, higher variance, higher mean
            if (strip_analysis['concentration'] < 0.85 or 
                strip_analysis['std_dev'] > 8 or 
                strip_analysis['mean'] > 12):
                edge_confidence[edge] = 0
                edge_details[edge] = {
                    'passes': 0, 
                    'spatial': 0, 
                    'symmetry': 0, 
                    'rejected': 'histogram_variance',
                    'concentration': strip_analysis['concentration'],
                    'std_dev': strip_analysis['std_dev'],
                    'mean': strip_analysis['mean']
                }
                continue
            
            # Count how many thresholds found continuous blanking
            passes_detected = sum(1 for cw in continuous_widths if cw >= min_blanking)
            pass_score = passes_detected * 17.5  # 0-70% (17.5 per pass)
            
            # Spatial consistency (already done in _get_continuous_blanking_width)
            spatial_score = 20.0  # Full 20% since continuous width was found
            
            # Total confidence for this edge (before symmetry and histogram bonuses)
            confidence = pass_score + spatial_score  # Max 90% before bonuses
            
            # Histogram bonus: very confident if high concentration + low variance
            histogram_bonus = 0.0
            if strip_analysis['concentration'] > 0.95 and strip_analysis['std_dev'] < 3:
                histogram_bonus = 10.0  # Very confident - true broadcast black
            elif strip_analysis['concentration'] > 0.90 and strip_analysis['std_dev'] < 5:
                histogram_bonus = 5.0  # Fairly confident
            
            confidence += histogram_bonus
            
            edge_confidence[edge] = confidence
            edge_details[edge] = {
                'passes': passes_detected,
                'spatial': spatial_score,
                'width': width,
                'histogram_bonus': histogram_bonus,
                'concentration': strip_analysis['concentration'],
                'std_dev': strip_analysis['std_dev'],
                'mean': strip_analysis['mean']
            }
            
            # Store width if confidence is sufficient
            if confidence >= self.CONFIDENCE_ORANGE:
                result[edge] = width
        
        # Symmetry bonus (applied to overall result) - 10% max
        symmetry_score = self._check_symmetry(result) * 10
        
        # Add symmetry to all edges that have blanking, cap at 100%
        for edge in ['left', 'right', 'top', 'bottom']:
            if result[edge] > 0:
                edge_confidence[edge] = min(100, edge_confidence[edge] + symmetry_score)
                edge_details[edge]['symmetry'] = symmetry_score
        
        # Determine overall confidence and severity (capped at 100%)
        max_confidence = min(100, max(edge_confidence.values())) if edge_confidence else 0
        
        result['has_blanking'] = any(result[e] > 0 for e in ['left', 'right', 'top', 'bottom'])
        result['confidence'] = max_confidence
        result['confidence_details'] = edge_details
        
        # Severity: RED (certified) or ORANGE (probable)
        if max_confidence >= self.CONFIDENCE_RED:
            result['severity'] = 'red'
        elif max_confidence >= self.CONFIDENCE_ORANGE:
            result['severity'] = 'orange'
        else:
            result['severity'] = 'none'
            result['has_blanking'] = False  # Below threshold = ignore
        
        return result
    
    def _is_shot_change(self, prev_gray, curr_gray) -> bool:
        """Detect if there's a shot/scene change between two frames.
        
        Uses histogram comparison - significant difference indicates cut.
        """
        if prev_gray is None or curr_gray is None:
            return True
        
        # Calculate histograms
        hist1 = cv2.calcHist([prev_gray], [0], None, [64], [0, 256])
        hist2 = cv2.calcHist([curr_gray], [0], None, [64], [0, 256])
        
        # Normalize
        cv2.normalize(hist1, hist1)
        cv2.normalize(hist2, hist2)
        
        # Compare using correlation (1.0 = identical, 0 = different)
        correlation = cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL)
        
        # If correlation is low, it's a shot change
        return correlation < 0.7
    
    def _detect_letterbox_at_frame(self, cap, frame_num: int) -> Dict:
        """Get letterbox values at a specific frame using widest threshold."""
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
        ret, frame = cap.read()
        if not ret:
            return {'left': 0, 'right': 0, 'top': 0, 'bottom': 0}
        
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        scan_depth = min(w // 4, h // 4)
        
        # Use widest threshold for letterbox detection
        return self._detect_blanking_at_threshold(gray, 80, scan_depth)
    
    def _detect_letterbox_segments(self, cap, total_frames: int, start_frame: int = 0) -> List[Dict]:
        """Detect letterbox segments - find exact frames where letterboxing changes.
        
        Returns list of segments: [{'start': frame, 'end': frame, 'baseline': {...}}, ...]
        """
        # Sample every 5 seconds (or 10% of video, whichever is smaller)
        sample_interval = min(int(self.fps * 5), max(1, total_frames // 10))
        
        samples = []  # List of (frame_num, letterbox_values)
        
        for frame_num in range(start_frame, total_frames, sample_interval):
            lb = self._detect_letterbox_at_frame(cap, frame_num)
            samples.append((frame_num, lb))
        
        if not samples:
            return [{'start': start_frame, 'end': total_frames, 
                    'baseline': {'left': 0, 'right': 0, 'top': 0, 'bottom': 0}}]
        
        # Find segment boundaries (where letterbox changes by >3px on any edge)
        segments = []
        segment_start = start_frame
        prev_lb = samples[0][1]
        
        for frame_num, lb in samples[1:]:
            # Check if letterbox changed significantly
            changed = False
            for edge in ['left', 'right', 'top', 'bottom']:
                if abs(lb[edge] - prev_lb[edge]) > 3:
                    changed = True
                    break
            
            if changed:
                # Find exact change frame using binary search
                exact_change = self._find_letterbox_change_frame(
                    cap, samples[samples.index((frame_num, lb)) - 1][0], frame_num, prev_lb
                )
                
                # Close previous segment
                segments.append({
                    'start': segment_start,
                    'end': exact_change - 1,
                    'baseline': prev_lb
                })
                
                # Start new segment
                segment_start = exact_change
                prev_lb = lb
        
        # Close final segment
        segments.append({
            'start': segment_start,
            'end': total_frames - 1,
            'baseline': prev_lb
        })
        
        return segments
    
    def _find_letterbox_change_frame(self, cap, start: int, end: int, prev_lb: Dict) -> int:
        """Binary search to find exact frame where letterbox changes."""
        while end - start > 1:
            mid = (start + end) // 2
            lb = self._detect_letterbox_at_frame(cap, mid)
            
            # Check if different from previous
            different = False
            for edge in ['left', 'right', 'top', 'bottom']:
                if abs(lb[edge] - prev_lb[edge]) > 3:
                    different = True
                    break
            
            if different:
                end = mid
            else:
                start = mid
        
        return end
    
    def _enforce_letterbox_symmetry(self, lb: Dict) -> Dict:
        """Enforce symmetry for letterbox baseline.
        
        True letterbox/pillarbox is SYMMETRIC:
        - Letterbox: Top AND Bottom (widescreen in 4:3)
        - Pillarbox: Left AND Right (4:3 in widescreen)
        
        A single-edge dark bar is NOT letterbox - it's potential blanking!
        """
        result = {'left': 0, 'right': 0, 'top': 0, 'bottom': 0}
        
        # Check horizontal symmetry (pillarbox): both L and R must be present
        left, right = lb.get('left', 0), lb.get('right', 0)
        if left > 0 and right > 0:
            if abs(left - right) <= max(5, min(left, right) * 0.2):
                # Symmetric - this is pillarbox, use smaller value as baseline
                result['left'] = min(left, right)
                result['right'] = min(left, right)
        
        # Check vertical symmetry (letterbox): both T and B must be present
        top, bottom = lb.get('top', 0), lb.get('bottom', 0)
        if top > 0 and bottom > 0:
            if abs(top - bottom) <= max(5, min(top, bottom) * 0.2):
                # Symmetric - this is letterbox
                result['top'] = min(top, bottom)
                result['bottom'] = min(top, bottom)
        
        return result
    
    def _get_letterbox_baseline_for_frame(self, frame_num: int) -> Dict:
        """Get the appropriate letterbox baseline for a specific frame.
        
        Enforces symmetry - single-edge dark bars are NOT considered letterbox.
        """
        for seg in self.letterbox_segments:
            if seg['start'] <= frame_num <= seg['end']:
                # Apply symmetry enforcement - single-edge != letterbox
                return self._enforce_letterbox_symmetry(seg['baseline'])
        return {'left': 0, 'right': 0, 'top': 0, 'bottom': 0}
    
    def _detect_letterboxing(self, cap, total_frames: int, start_frame: int = 0) -> Dict:
        """Detect consistent letterboxing/pillarboxing by sampling frames.
        
        Also detects letterbox segments (for videos with changing aspect ratios).
        Returns the primary letterbox baseline.
        """
        # Detect all letterbox segments
        self.letterbox_segments = self._detect_letterbox_segments(cap, total_frames, start_frame)
        
        # Return the letterbox baseline of the longest segment
        if self.letterbox_segments:
            longest = max(self.letterbox_segments, key=lambda s: s['end'] - s['start'])
            baseline = longest['baseline'].copy()
            baseline['has_letterbox'] = any([
                baseline['left'] > 0, baseline['right'] > 0,
                baseline['top'] > 0, baseline['bottom'] > 0
            ])
            baseline['segments'] = self.letterbox_segments
            return baseline
        
        return {'left': 0, 'right': 0, 'top': 0, 'bottom': 0, 'has_letterbox': False}
    
    def _scan_all_frames_for_blanking(self, cap, start_frame: int, letterbox_baseline=None, progress_callback=None) -> List[Dict]:
        """Scan every frame for blanking with confidence scoring.
        
        Uses letterbox segments for frame-specific baselines.
        Returns segments with confidence scores and human-readable details.
        """
        raw_segments = []
        current_segment = None
        prev_gray = None
        shot_boundaries = []
        frames_to_scan = self.total_frames - start_frame
        
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        frame_num = start_frame
        
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            
            # Report progress
            if progress_callback and (frame_num - start_frame) % 50 == 0:
                pct = int(((frame_num - start_frame) / frames_to_scan) * 100)
                progress_callback('blanking', pct)
            
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            
            # Detect shot changes
            if self._is_shot_change(prev_gray, gray):
                shot_boundaries.append(frame_num)
            
            # Get frame-specific letterbox baseline
            lb = self._get_letterbox_baseline_for_frame(frame_num)
            
            # Detect blanking with confidence scoring
            result = self._detect_blanking_frame(frame, lb, gray=gray)
            
            # Debug mode: track the frame with highest blanking confidence
            if self.debug_mode and result['has_blanking']:
                if self.worst_frame_data is None or result['confidence'] > self.worst_frame_data['confidence']:
                    # Collect strip analysis for each edge with blanking
                    strip_analyses = {}
                    for edge in ['left', 'right', 'top', 'bottom']:
                        if result.get(edge, 0) > 0:
                            strip_analyses[edge] = self._analyze_blanking_strip(gray, edge, result[edge])
                    
                    self.worst_frame_data = {
                        'frame_num': frame_num,
                        'timecode': self._frames_to_tc(frame_num),
                        'confidence': result['confidence'],
                        'severity': result['severity'],
                        'blanking': {k: v for k, v in result.items() 
                                    if k in ['left', 'right', 'top', 'bottom']},
                        'confidence_details': result.get('confidence_details', {}),
                        'strip_analysis': strip_analyses,
                        'frame_image': frame.copy(),
                        'gray': gray.copy()
                    }
            
            if result['has_blanking']:
                if current_segment is None:
                    current_segment = {
                        'start_frame': frame_num,
                        'end_frame': frame_num,
                        'start_tc': self._frames_to_tc(frame_num),
                        'blanking': result,
                        'confidence': result['confidence'],
                        'severity': result['severity'],
                        'confidence_details': result.get('confidence_details', {})
                    }
                else:
                    current_segment['end_frame'] = frame_num
                    # Update max values
                    for edge in ['left', 'right', 'top', 'bottom']:
                        current_segment['blanking'][edge] = max(
                            current_segment['blanking'].get(edge, 0),
                            result.get(edge, 0)
                        )
                    # Keep highest confidence
                    if result['confidence'] > current_segment['confidence']:
                        current_segment['confidence'] = result['confidence']
                        current_segment['severity'] = result['severity']
                        current_segment['confidence_details'] = result.get('confidence_details', {})
            else:
                if current_segment is not None:
                    current_segment['end_tc'] = self._frames_to_tc(current_segment['end_frame'])
                    current_segment['duration_frames'] = current_segment['end_frame'] - current_segment['start_frame'] + 1
                    raw_segments.append(current_segment)
                    current_segment = None
            
            prev_gray = gray
            frame_num += 1
        
        if progress_callback:
            progress_callback('blanking', 100)
        
        # Close last segment
        if current_segment is not None:
            current_segment['end_tc'] = self._frames_to_tc(current_segment['end_frame'])
            current_segment['duration_frames'] = current_segment['end_frame'] - current_segment['start_frame'] + 1
            raw_segments.append(current_segment)
        
        # Merge segments in same shot
        merged_segments = self._merge_segments_by_shot(raw_segments, shot_boundaries)
        
        # Add human-readable confidence breakdown to each segment
        for seg in merged_segments:
            seg['confidence_text'] = self._format_confidence_breakdown(seg)
        
        return merged_segments
    
    def _format_confidence_breakdown(self, segment: Dict) -> str:
        """Format confidence details for human-readable output."""
        details = segment.get('confidence_details', {})
        severity = segment.get('severity', 'none')
        
        # Severity indicator
        if severity == 'red':
            header = "🔴 BLANKING (HIGH confidence)"
        elif severity == 'orange':
            header = "🟠 BLANKING (needs review)"
        else:
            header = "— No blanking"
        
        lines = [header]
        
        # Edge details
        blanking = segment.get('blanking', {})
        for edge in ['left', 'right', 'top', 'bottom']:
            width = blanking.get(edge, 0)
            if width > 0:
                edge_detail = details.get(edge, {})
                passes = edge_detail.get('passes', 0)
                spatial = edge_detail.get('spatial', 0)
                symmetry = edge_detail.get('symmetry', 0)
                
                edge_name = edge.capitalize()
                lines.append(f"  {edge_name}: {width}px")
                
                # Confidence factors
                factors = []
                if passes >= 3:
                    factors.append("✓ Multi-pass confirmed")
                elif passes >= 2:
                    factors.append("~ Partial confirmation")
                
                if spatial >= 15:
                    factors.append("✓ Consistent width")
                elif spatial >= 10:
                    factors.append("~ Mostly consistent")
                
                if symmetry >= 8:
                    factors.append("✓ Symmetric")
                
                for f in factors:
                    lines.append(f"    {f}")
        
        return '\n'.join(lines)
    
    def _merge_segments_by_shot(self, segments: List[Dict], shot_boundaries: List[int]) -> List[Dict]:
        """Merge ALL blanking segments within the same shot into one flag.
        
        Logic:
        - Same shot: merge everything (L, R, B, T) into ONE segment
        - Different shot: create separate segment
        - If no shot boundaries: merge consecutive segments (within 30 frame gap)
        """
        if not segments:
            return segments
        
        merged = []
        current_merged = None
        
        def get_shot_idx(frame_num):
            """Get which shot a frame belongs to."""
            if not shot_boundaries:
                return -1  # Special value: no shot detection
            for i, boundary in enumerate(shot_boundaries):
                if frame_num < boundary:
                    return i
            return len(shot_boundaries)
        
        for seg in segments:
            seg_shot = get_shot_idx(seg['start_frame'])
            
            if current_merged is None:
                current_merged = seg.copy()
                current_merged['shot_idx'] = seg_shot
            else:
                # Determine if we should merge
                if seg_shot == -1:
                    # No shot detection - merge if within 30 frames
                    should_merge = (seg['start_frame'] - current_merged['end_frame'] <= 30)
                else:
                    # Shot detection available - merge if same shot
                    should_merge = (seg_shot == current_merged['shot_idx'])
                
                if should_merge:
                    # Same shot (or close enough) - merge all edges
                    current_merged['end_frame'] = seg['end_frame']
                    current_merged['end_tc'] = seg['end_tc']
                    current_merged['duration_frames'] = current_merged['end_frame'] - current_merged['start_frame'] + 1
                    # Keep max blanking values for all edges
                    for edge in ['left', 'right', 'top', 'bottom']:
                        current_merged['blanking'][edge] = max(
                            current_merged['blanking'].get(edge, 0),
                            seg['blanking'].get(edge, 0)
                        )
                    # Keep highest confidence
                    current_merged['confidence'] = max(
                        current_merged.get('confidence', 0),
                        seg.get('confidence', 0)
                    )
                else:
                    # Different shot - save current and start new
                    merged.append(current_merged)
                    current_merged = seg.copy()
                    current_merged['shot_idx'] = seg_shot
        
        if current_merged:
            merged.append(current_merged)
        
        return merged
    
    def analyze(self, progress_callback=None) -> Dict:
        """Single-pass analysis for slate, duplicates, letterboxing, and blanking.
        
        Reads each frame only once (after slate detection).
        Letterbox detection is adaptive — updates at shot boundaries and
        periodically, handling non-constant pillarboxing/letterboxing.
        
        Args:
            progress_callback: Optional function(phase: str, progress: int) 
                phase: 'scanning'
                progress: 0-100
        
        Returns:
            Dict with analysis results including slate info, duplicates, and blanking.
        """
        if not HAS_CV2:
            return {'slate_detected': False, 'duplicate_frames': [], 'error': 'OpenCV not installed'}
        
        try:
            cap = cv2.VideoCapture(self.video_path)
            if not cap.isOpened():
                return {'error': 'Could not open video'}
            
            self.fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            
            # ─── PHASE 1: Slate detection (first ~15 s) ───
            scan_frames = int(min(15 * self.fps, self.total_frames))
            slate_end = 0
            black_frame_count = 0
            
            if self.custom_duration:
                slate_end = int(self.custom_duration * self.fps)
            else:
                last_slate_frame = 0
                first_content_frame = 0
                in_black_section = False
                
                for i in range(scan_frames):
                    ret, frame = cap.read()
                    if not ret:
                        break
                    
                    if self._is_slate_frame(frame):
                        last_slate_frame = i
                        in_black_section = False
                    elif self._is_black_frame(frame):
                        if last_slate_frame > 0:
                            black_frame_count += 1
                            in_black_section = True
                    else:
                        if last_slate_frame > 0 or in_black_section:
                            first_content_frame = i
                            break
                
                if first_content_frame > 0:
                    slate_end = first_content_frame
                elif last_slate_frame > 0:
                    slate_end = last_slate_frame + black_frame_count + 1
            
            self.slate_end_frame = slate_end
            self.slate_duration = slate_end / self.fps if slate_end > 0 else 0
            
            # ─── PHASE 2: Single-pass scan (duplicates + blanking + letterbox) ───
            #
            # Uses a threaded frame reader (producer-consumer) so that video
            # decoding overlaps with frame analysis — typically ~30-40% faster.
            scan_start = max(0, slate_end if slate_end > 0 else 0)
            
            downscale = app_settings.get('qc_downscale', 0.85)
            
            # Duplicate detection state
            prev_gray_small = None
            content_mse = []
            small_dims = None
            
            # Blanking detection state
            blanking_raw = []
            cur_blanking_seg = None
            prev_gray_shot = None
            shot_boundaries = []
            
            # Adaptive letterbox state (handles non-constant pillarboxing)
            lb_interval = max(1, int(self.fps // 5))   # update ~5×/sec
            cur_lb = {'left': 0, 'right': 0, 'top': 0, 'bottom': 0}
            
            frames_to_scan = max(1, self.total_frames - scan_start)
            
            # ── Threaded frame reader ──
            # Decodes up to 8 frames ahead while the main thread analyses.
            frame_q = queue.Queue(maxsize=8)
            
            def _read_frames(cap_obj, start, count):
                """Background thread: sequential read into bounded queue."""
                cap_obj.set(cv2.CAP_PROP_POS_FRAMES, start)
                for i in range(count):
                    ret, frm = cap_obj.read()
                    if not ret:
                        break
                    frame_q.put((start + i, frm))
                frame_q.put(None)  # sentinel — signals end of stream
            
            reader_thread = threading.Thread(
                target=_read_frames, args=(cap, scan_start, frames_to_scan), daemon=True
            )
            reader_thread.start()
            
            while True:
                item = frame_q.get()
                if item is None:
                    break
                frame_num, frame = item
                
                # Progress
                if progress_callback and (frame_num - scan_start) % 50 == 0:
                    pct = int(((frame_num - scan_start) / frames_to_scan) * 100)
                    progress_callback('scanning', pct)
                
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
                h, w = gray.shape[:2]
                
                # ── Shot-change detection ──
                is_new_shot = self._is_shot_change(prev_gray_shot, gray)
                if is_new_shot:
                    shot_boundaries.append(frame_num)
                
                # ── Adaptive letterbox (at shot boundaries + periodically) ──
                if is_new_shot or (frame_num - scan_start) % lb_interval == 0:
                    lb_depth = min(w // 4, h // 4)
                    raw_lb = self._detect_blanking_at_threshold(gray, 80, lb_depth)
                    cur_lb = self._enforce_letterbox_symmetry(raw_lb)
                
                # ── Duplicate detection (downscaled gray) ──
                if small_dims is None and downscale < 1.0:
                    small_dims = (int(w * downscale), int(h * downscale))
                
                if downscale < 1.0 and small_dims:
                    gray_small = cv2.resize(gray, small_dims, interpolation=cv2.INTER_AREA)
                else:
                    gray_small = gray
                
                if prev_gray_small is not None:
                    mse = float(np.mean((prev_gray_small.astype(float) - gray_small.astype(float)) ** 2))
                    content_mse.append((frame_num, mse))
                prev_gray_small = gray_small
                
                # ── Blanking detection (full resolution, adaptive letterbox) ──
                result = self._detect_blanking_frame(frame, cur_lb, gray=gray)
                
                # Debug mode: track worst frame
                if self.debug_mode and result['has_blanking']:
                    if self.worst_frame_data is None or result['confidence'] > self.worst_frame_data['confidence']:
                        strip_analyses = {}
                        for edge in ['left', 'right', 'top', 'bottom']:
                            if result.get(edge, 0) > 0:
                                strip_analyses[edge] = self._analyze_blanking_strip(gray, edge, result[edge])
                        self.worst_frame_data = {
                            'frame_num': frame_num,
                            'timecode': self._frames_to_tc(frame_num),
                            'confidence': result['confidence'],
                            'severity': result['severity'],
                            'blanking': {k: v for k, v in result.items()
                                        if k in ['left', 'right', 'top', 'bottom']},
                            'confidence_details': result.get('confidence_details', {}),
                            'strip_analysis': strip_analyses,
                            'frame_image': frame.copy(),
                            'gray': gray.copy()
                        }
                
                if result['has_blanking']:
                    if cur_blanking_seg is None:
                        cur_blanking_seg = {
                            'start_frame': frame_num,
                            'end_frame': frame_num,
                            'start_tc': self._frames_to_tc(frame_num),
                            'blanking': result,
                            'confidence': result['confidence'],
                            'severity': result['severity'],
                            'confidence_details': result.get('confidence_details', {})
                        }
                    else:
                        cur_blanking_seg['end_frame'] = frame_num
                        for edge in ['left', 'right', 'top', 'bottom']:
                            cur_blanking_seg['blanking'][edge] = max(
                                cur_blanking_seg['blanking'].get(edge, 0),
                                result.get(edge, 0)
                            )
                        if result['confidence'] > cur_blanking_seg['confidence']:
                            cur_blanking_seg['confidence'] = result['confidence']
                            cur_blanking_seg['severity'] = result['severity']
                            cur_blanking_seg['confidence_details'] = result.get('confidence_details', {})
                else:
                    if cur_blanking_seg is not None:
                        cur_blanking_seg['end_tc'] = self._frames_to_tc(cur_blanking_seg['end_frame'])
                        cur_blanking_seg['duration_frames'] = cur_blanking_seg['end_frame'] - cur_blanking_seg['start_frame'] + 1
                        blanking_raw.append(cur_blanking_seg)
                        cur_blanking_seg = None
                
                prev_gray_shot = gray
            
            reader_thread.join()  # ensure reader is done before releasing cap
            
            # Close last blanking segment
            if cur_blanking_seg is not None:
                cur_blanking_seg['end_tc'] = self._frames_to_tc(cur_blanking_seg['end_frame'])
                cur_blanking_seg['duration_frames'] = cur_blanking_seg['end_frame'] - cur_blanking_seg['start_frame'] + 1
                blanking_raw.append(cur_blanking_seg)
            
            if progress_callback:
                progress_callback('scanning', 100)
            
            cap.release()
            
            # ── Post-process duplicates ──
            duplicates = []
            dup_threshold = 20
            for idx in range(1, len(content_mse) - 1):
                fn, curr_mse = content_mse[idx]
                _, prev_mse = content_mse[idx - 1]
                _, next_mse = content_mse[idx + 1]
                if (curr_mse < dup_threshold and
                    prev_mse > curr_mse * 5 and
                    next_mse > curr_mse * 5):
                    duplicates.append({
                        'frame': fn,
                        'timecode': self._frames_to_tc(fn),
                        'mse': curr_mse
                    })
            
            # ── Post-process blanking ──
            merged_blanking = self._merge_segments_by_shot(blanking_raw, shot_boundaries)
            for seg in merged_blanking:
                seg['confidence_text'] = self._format_confidence_breakdown(seg)
            
            self.blanking_segments = merged_blanking
            self.duplicate_frames = duplicates
            
            # ── Letterbox summary ──
            letterbox = cur_lb.copy()
            letterbox['has_letterbox'] = any(letterbox.get(e, 0) > 0 for e in ['left', 'right', 'top', 'bottom'])
            
            # ── Blanking summary ──
            has_blanking = len(merged_blanking) > 0
            max_blanking = {'left': 0, 'right': 0, 'top': 0, 'bottom': 0}
            total_blanking_frames = 0
            
            for seg in merged_blanking:
                total_blanking_frames += seg['duration_frames']
                b = seg['blanking']
                for edge in ['left', 'right', 'top', 'bottom']:
                    max_blanking[edge] = max(max_blanking[edge], b.get(edge, 0))
            
            return {
                'slate_detected': slate_end > 0,
                'slate_duration': self.slate_duration,
                'slate_end_frame': self.slate_end_frame,
                'slate_end_timecode': self._frames_to_tc(self.slate_end_frame),
                'duplicate_frames': duplicates,
                'letterbox': letterbox,
                'blanking': {
                    'has_blanking': has_blanking,
                    'segments': merged_blanking,
                    'total_frames': total_blanking_frames,
                    'max': max_blanking
                },
                'fps': self.fps
            }
            
        except Exception as e:
            return {'error': str(e)}
    
    def _frames_to_tc(self, frame_num: int) -> str:
        """Convert frame number to timecode string HH:MM:SS:FF."""
        total_seconds = frame_num / self.fps
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        seconds = int(total_seconds % 60)
        frames = int(frame_num % self.fps)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"
    
    def save_debug_report(self, output_dir: Optional[str] = None) -> Optional[str]:
        """Save debug report with worst frame screenshot and analysis.
        
        Args:
            output_dir: Directory to save report. Uses self.debug_output_dir if not provided.
        
        Returns:
            Path to the saved report, or None if no debug data available.
        """
        import os
        
        if not HAS_CV2:
            return None
        
        if self.worst_frame_data is None:
            return None
        
        # Determine output directory
        out_dir = output_dir or self.debug_output_dir
        if out_dir is None:
            # Default to same directory as video
            out_dir = os.path.dirname(self.video_path)
        
        os.makedirs(out_dir, exist_ok=True)
        
        # Generate base filename from video
        video_name = os.path.splitext(os.path.basename(self.video_path))[0]
        base_name = f"{video_name}_blanking_debug"
        
        data = self.worst_frame_data
        frame = data['frame_image']
        h, w = frame.shape[:2]
        
        # === Create annotated frame image ===
        annotated = frame.copy()
        
        # Draw blanking regions with colored overlays
        blanking = data['blanking']
        overlay_color = (0, 165, 255) if data['severity'] == 'orange' else (0, 0, 255)  # Orange or Red (BGR)
        
        if blanking.get('left', 0) > 0:
            cv2.rectangle(annotated, (0, 0), (blanking['left'], h), overlay_color, 2)
            cv2.putText(annotated, f"L:{blanking['left']}px", (5, 30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, overlay_color, 2)
        
        if blanking.get('right', 0) > 0:
            cv2.rectangle(annotated, (w - blanking['right'], 0), (w, h), overlay_color, 2)
            cv2.putText(annotated, f"R:{blanking['right']}px", (w - 100, 30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, overlay_color, 2)
        
        if blanking.get('top', 0) > 0:
            cv2.rectangle(annotated, (0, 0), (w, blanking['top']), overlay_color, 2)
            cv2.putText(annotated, f"T:{blanking['top']}px", (w//2 - 40, blanking['top'] + 25), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, overlay_color, 2)
        
        if blanking.get('bottom', 0) > 0:
            cv2.rectangle(annotated, (0, h - blanking['bottom']), (w, h), overlay_color, 2)
            cv2.putText(annotated, f"B:{blanking['bottom']}px", (w//2 - 40, h - blanking['bottom'] - 10), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, overlay_color, 2)
        
        # Add frame info at bottom
        info_text = f"Frame {data['frame_num']} | {data['timecode']} | Confidence: {data['confidence']:.1f}%"
        cv2.putText(annotated, info_text, (10, h - 10), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        
        # Save annotated frame
        frame_path = os.path.join(out_dir, f"{base_name}_frame.png")
        cv2.imwrite(frame_path, annotated)
        
        # === Create text report ===
        report_lines = [
            "=" * 60,
            "BLANKING DEBUG REPORT",
            "=" * 60,
            "",
            f"Video: {self.video_path}",
            f"Frame: {data['frame_num']} ({data['timecode']})",
            f"Confidence: {data['confidence']:.1f}%",
            f"Severity: {data['severity'].upper()}",
            "",
            "-" * 40,
            "BLANKING DETECTED",
            "-" * 40,
        ]
        
        for edge in ['left', 'right', 'top', 'bottom']:
            width = blanking.get(edge, 0)
            if width > 0:
                report_lines.append(f"\n{edge.upper()}: {width}px")
                
                # Add strip analysis if available
                strip = data.get('strip_analysis', {}).get(edge, {})
                if strip:
                    report_lines.append(f"  Concentration: {strip.get('concentration', 0)*100:.1f}% in [0-16]")
                    report_lines.append(f"  Std Dev: {strip.get('std_dev', 0):.2f}")
                    report_lines.append(f"  Mean: {strip.get('mean', 0):.2f}")
                    report_lines.append(f"  Variance: {strip.get('variance', 0):.2f}")
                    
                    # Histogram visualization (text-based)
                    hist = strip.get('histogram', [])
                    if hist:
                        report_lines.append("  Histogram (16 bins, 0-255):")
                        max_hist = max(hist) if hist else 1
                        for i, count in enumerate(hist):
                            bar_len = int((count / max_hist) * 30) if max_hist > 0 else 0
                            range_start = i * 16
                            range_end = range_start + 15
                            report_lines.append(f"    [{range_start:3d}-{range_end:3d}]: {'█' * bar_len} ({count})")
                
                # Add confidence details
                details = data.get('confidence_details', {}).get(edge, {})
                if details:
                    report_lines.append(f"  Passes detected: {details.get('passes', 0)}/4")
                    report_lines.append(f"  Spatial score: {details.get('spatial', 0):.1f}")
                    report_lines.append(f"  Histogram bonus: {details.get('histogram_bonus', 0):.1f}")
        
        report_lines.extend([
            "",
            "-" * 40,
            "INTERPRETATION",
            "-" * 40,
            "",
            "Real blanking indicators:",
            "  ✓ Concentration > 90% (pixels in [0-16])",
            "  ✓ Std Dev < 5 (uniform darkness)",
            "  ✓ Mean < 10 (true broadcast black)",
            "",
            "Dark content indicators:",
            "  ✗ Concentration < 80%",
            "  ✗ Std Dev > 8 (variable darkness)",
            "  ✗ Mean > 20 (dark gray, not black)",
            "",
            "=" * 60,
        ])
        
        # Save text report
        report_path = os.path.join(out_dir, f"{base_name}_report.txt")
        with open(report_path, 'w') as f:
            f.write('\n'.join(report_lines))
        
        return report_path


def is_slate_frame(frame) -> bool:
    """Standalone slate detection (cream top, teal bottom) without full SlateDetector."""
    h, w = frame.shape[:2]
    top_region = frame[0:int(h*0.4), int(w*0.3):int(w*0.7)]
    bottom_region = frame[int(h*0.7):h, int(w*0.3):int(w*0.7)]
    top_mean = np.mean(top_region, axis=(0, 1))
    is_cream = (top_mean[0] > 200 and top_mean[1] > 210 and top_mean[2] > 210 and
               top_mean[2] > top_mean[0])
    bottom_mean = np.mean(bottom_region, axis=(0, 1))
    is_teal = (bottom_mean[1] > bottom_mean[2] and bottom_mean[1] > 100 and bottom_mean[0] > 80)
    return is_cream and is_teal