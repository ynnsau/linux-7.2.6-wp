import java.io.File;
import java.util.Locale;
import org.HdrHistogram.Histogram;
import org.HdrHistogram.HistogramLogReader;
import org.HdrHistogram.EncodableHistogram;

/** Merge recorded one-second YCSB latency histograms for the selected window. */
public class YcsbHdrWindow {
  public static void main(String[] args) throws Exception {
    Locale.setDefault(Locale.US);
    long from = Long.parseLong(args[1]);
    long to = Long.parseLong(args[2]);
    StringBuilder out = new StringBuilder("{");
    boolean first = true;
    for (String op : new String[]{"READ", "UPDATE"}) {
      File file = new File(args[0], op + ".hdr");
      if (!file.exists()) continue;
      Histogram total = null;
      int intervals = 0;
      long start = Long.MAX_VALUE, end = 0;
      try (HistogramLogReader reader = new HistogramLogReader(file)) {
        EncodableHistogram item;
        while ((item = reader.nextIntervalHistogram()) != null) {
          Histogram h = (Histogram)item;
          // Status count boundaries precede histogram rotation by a few ms.
          // A 100ms tolerance includes the matching end interval, not the warm-up one.
          if (h.getEndTimeStamp() > from + 100 && h.getEndTimeStamp() <= to + 100) {
            if (total == null) total = h.copy(); else total.add(h);
            intervals++;
            start = Math.min(start, h.getStartTimeStamp());
            end = Math.max(end, h.getEndTimeStamp());
          }
        }
      }
      if (total == null || total.getTotalCount() == 0) throw new IllegalStateException("Empty " + op + " window");
      if (!first) out.append(',');
      first = false;
      out.append(String.format("\"%s\":{\"count\":%d,\"intervals\":%d,\"start_ms\":%d,\"end_ms\":%d,\"mean_us\":%.4f,\"p50_us\":%d,\"p95_us\":%d,\"p99_us\":%d,\"p999_us\":%d,\"max_us\":%d}",
          op, total.getTotalCount(), intervals, start, end, total.getMean(),
          total.getValueAtPercentile(50), total.getValueAtPercentile(95), total.getValueAtPercentile(99),
          total.getValueAtPercentile(99.9), total.getMaxValue()));
    }
    System.out.println(out.append('}').toString());
  }
}
