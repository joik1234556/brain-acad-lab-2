package ua.zp.brainacad;

public class lesson3 {
    public static void main(String[] args) {
        /**
         * задание №1
         */
        byte a = 10;
        short b = 100;
        int c = 1000;
        long d = 10_000;
        float e = 100_000;
        double i = 100_000_0;
        char o = '1';
        boolean z = true;
        System.out.println("byte=" + a + "\nshort=" + b + "\nint=" + c + "\nlong=" + d + "\nfloat=" + e + "\ndouble=" + i + "\nchar=" + o + "\nboolean=" + z);
        /**
         * задание №2
         */
        short f = (short) (a);
        int j = (int) (f);
        long p = (long) (j);
        System.out.println(f + " " + j + " " + p);
        double e1 = (double) (c);
        float b1 = (float) (b);
        double e2 = (double) (e);
        int c1 = (int) (o);
        System.out.println(e1 + " " + b1 + " " + e2 + " " + c1);

    }


}
